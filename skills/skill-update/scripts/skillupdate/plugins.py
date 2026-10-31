"""Claude Code plugins that ship skills.

Claude Code owns plugin installs, so updating is delegated to its own `claude plugin update`.
What we add is a best-effort check: when a marketplace catalog publishes a version, compare it with
the installed one. Plugins whose catalog publishes no version are handed to Claude Code too, which
knows whether anything newer exists; afterwards we compare the registry to see what really changed.
"""
from __future__ import annotations

import os
import re
import shutil
import time
from calendar import timegm
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .backup import Run
from .context import Ctx
from .model import BLOCKED, CURRENT, KIND_PLUGIN, OUTDATED, UNKNOWN, Item, Result
from .util import first_error, home, read_json, run as run_cmd, scrub, short

STALE_CATALOG_DAYS = 7
_KEY_OK = re.compile(r"^[\w][\w.@-]*$")  # name@marketplace; must not look like a command line option


def config_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else home() / ".claude"


def claude_bin() -> Optional[str]:
    return os.environ.get("SKILL_UPDATE_CLAUDE") or shutil.which("claude")


def _installed() -> Dict[str, List[Dict[str, Any]]]:
    data = read_json(config_dir() / "plugins" / "installed_plugins.json")
    plugins = data.get("plugins") if isinstance(data, dict) else None
    return plugins if isinstance(plugins, dict) else {}


def _markets() -> Dict[str, Dict[str, Any]]:
    data = read_json(config_dir() / "plugins" / "known_marketplaces.json")
    return data if isinstance(data, dict) else {}


def _catalog_lookup(market: Optional[Dict[str, Any]], name: str) -> Tuple[Optional[bool], Optional[str], Optional[str]]:
    """(is it listed, the version the catalog publishes, the name it was renamed to).

    The first value is None when the catalog itself could not be read, so nothing can be said."""
    if not market:
        return None, None, None
    root = Path(str(market.get("installLocation") or ""))
    catalog = read_json(root / ".claude-plugin" / "marketplace.json")
    entries = catalog.get("plugins") if isinstance(catalog, dict) else None
    if not isinstance(entries, list):
        return None, None, None
    entry = next((p for p in entries if isinstance(p, dict) and p.get("name") == name), None)
    if entry is None:
        renames = catalog.get("renames")
        renamed = renames.get(name) if isinstance(renames, dict) else None
        return False, None, str(renamed) if renamed else None
    if entry.get("version"):
        return True, str(entry["version"]), None
    source = entry.get("source")
    if isinstance(source, str):  # a folder inside the marketplace repository
        manifest = read_json((root / source / ".claude-plugin" / "plugin.json").resolve())
        if isinstance(manifest, dict) and manifest.get("version"):
            return True, str(manifest["version"]), None
    return True, None, None


def _age_days(iso: str) -> Optional[int]:
    try:
        return int((time.time() - timegm(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S"))) // 86400)
    except (ValueError, TypeError):
        return None


def refresh(ctx: Ctx) -> None:
    """Ask Claude Code to re-read every marketplace catalog (what it does itself on startup)."""
    binary = claude_bin()
    if not binary or ctx.plugins_refreshed:
        return
    ctx.plugins_refreshed = True
    r = run_cmd([binary, "plugin", "marketplace", "update"], timeout=900)
    if not r.ok:
        ctx.note("Could not refresh the plugin marketplaces: %s" % first_error(r))


def check(ctx: Ctx, do_refresh: bool = False) -> List[Item]:
    if not _installed():
        return []
    if do_refresh:
        refresh(ctx)
    installed, markets = _installed(), _markets()
    items: List[Item] = []
    oldest = 0
    for key in sorted(installed):
        entry = next((e for e in installed[key] if isinstance(e, dict) and e.get("scope", "user") == "user"), None)
        if entry is None:
            continue  # installed for one project only
        name, _, market_name = key.partition("@")
        version = str(entry.get("version") or "")
        listed, available, renamed_to = _catalog_lookup(markets.get(market_name), name)
        item = Item(name=key, kind=KIND_PLUGIN, status=UNKNOWN, source=market_name, installed=version,
                    latest=available or "", locations=[short(entry.get("installPath", ""))])
        if listed is False:  # nothing Claude Code can update: say so now instead of failing during the update
            item.status = BLOCKED
            item.notes.append(("renamed to %s in the %s marketplace; install that one and remove this one" % (renamed_to, market_name))
                              if renamed_to else
                              "no longer listed in the %s marketplace, so it cannot be updated (remove it with `claude plugin uninstall`)" % market_name)
        elif available is None:
            item.notes.append("no version in the catalog; Claude Code decides during the update")
        else:
            item.status = CURRENT if available == version else OUTDATED
        item.data = {"scope": entry.get("scope", "user"), "key": key}  # `key` is what gets acted on; `name` is only shown
        items.append(item)
        age = _age_days(str((markets.get(market_name) or {}).get("lastUpdated") or ""))
        oldest = max(oldest, age or 0)
    if oldest >= STALE_CATALOG_DAYS and not do_refresh:
        ctx.note("Some plugin marketplace catalogs were last refreshed %d days ago; add --refresh for an up-to-date plugin check." % oldest)
    return items


def _fingerprint(entries: Optional[List[Dict[str, Any]]]) -> Any:
    entry = next((e for e in entries or [] if isinstance(e, dict) and e.get("scope", "user") == "user"), {})
    return (entry.get("version"), entry.get("gitCommitSha"), entry.get("installPath"))


def apply(ctx: Ctx, run: Run, items: List[Item]) -> List[Result]:
    binary = claude_bin()
    if not binary:
        return [Result(i.name, KIND_PLUGIN, "failed", "the `claude` command was not found") for i in items]
    refresh(ctx)  # only here, after the user agreed: it writes to Claude Code's own marketplace folders
    before = _installed()
    results: List[Result] = []
    for it in items:
        key = it.data["key"]
        if not _KEY_OK.match(key) or str(it.data.get("scope")) not in ("user", "project", "local", "managed"):
            results.append(Result(it.name, KIND_PLUGIN, "failed", "unexpected plugin name or scope; refusing to pass it to claude"))
            continue
        r = run_cmd([binary, "plugin", "update", key, "--scope", str(it.data["scope"])], timeout=600)
        after = _fingerprint(_installed().get(key))
        if not r.ok:
            results.append(Result(it.name, KIND_PLUGIN, "failed", first_error(r)))
        elif after != _fingerprint(before.get(key)):
            old, new = _fingerprint(before.get(key))[0], after[0]
            results.append(Result(it.name, KIND_PLUGIN, "updated", "%s → %s" % (old, new) if old != new else "updated"))
        else:
            results.append(Result(it.name, KIND_PLUGIN, "unchanged", "already up to date"))
    if any(r.outcome == "updated" for r in results):
        ctx.note("Restart Claude Code to load the updated plugins.")
    return results
