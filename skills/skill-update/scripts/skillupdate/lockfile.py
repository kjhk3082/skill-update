"""Skills installed with `npx skills` (https://github.com/vercel-labs/skills).

The CLI records every install in ~/.agents/.skill-lock.json: the source repository, the path of the
skill inside it, and `skillFolderHash`, the git tree hash of that folder at install time. We compare
the installed folder with the newest upstream folder directly, so the answer stays right even when
the lock file and the files on disk have drifted apart. Updating replaces the folder and refreshes
the lock entry, so `npx skills check` agrees afterwards.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from .backup import Run, UpdateError, carry_local_state
from .context import Ctx
from .discover import Skill
from .gitops import GitError, RemoteCache, safe_folder
from .model import BLOCKED, CURRENT, ERROR, KIND_LOCK, OUTDATED, UNKNOWN, Item, Result
from .treehash import Manifest, cli_view, diff, filtered, manifest, tree_sha
from .util import atomic_write, frontmatter_name, home, now_iso, read_json, sanitize_name, scrub, short

LOCK_VERSION = 3
SELF = "skill-update"  # updated last, so the running tool is never replaced while other work is pending


@dataclass
class Upstream:
    """What the newest version of one lock entry looks like."""
    key: str
    entry: Dict[str, Any]
    cache: RemoteCache
    folder: str
    tree: str           # git's hash of the folder: what the lock file records
    kept: Manifest      # its files, minus the junk that is ignored on disk
    cmp: str            # hash of `kept`: what an installed folder is compared with
    cli_cmp: str        # hash of what `npx skills add` leaves on disk for this version
    cli_differs: bool   # the CLI would not reproduce the folder exactly (metadata.json, symlinks), so an
                        # edit cannot be told apart from what the CLI leaves out
    moved: bool

    @property
    def lock_hash(self) -> str:
        return str(self.entry.get("skillFolderHash") or "")


def lock_path() -> Optional[Path]:
    """The CLI's own rule: $XDG_STATE_HOME/skills/.skill-lock.json when that is set, else ~/.agents/.skill-lock.json."""
    if os.environ.get("SKILL_UPDATE_LOCK"):
        candidate = Path(os.environ["SKILL_UPDATE_LOCK"]).expanduser()
    elif os.environ.get("XDG_STATE_HOME"):
        candidate = Path(os.environ["XDG_STATE_HOME"]) / "skills" / ".skill-lock.json"
    else:
        candidate = home() / ".agents" / ".skill-lock.json"
    return candidate if candidate.is_file() else None


def _supported(entry: Dict[str, Any]) -> bool:
    return entry.get("sourceType") in ("github", "git") and bool(entry.get("sourceUrl"))


def _cache_key(entry: Dict[str, Any]) -> Tuple[Any, Any]:
    return entry.get("sourceUrl"), entry.get("ref")


def _compare_url(url: str, folder: str) -> str:
    m = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    if not m:
        return ""
    base = "https://github.com/%s/%s/commits/HEAD" % m.groups()
    return base + ("/" + folder if folder else "")


def _relocate(cache: RemoteCache, key: str) -> Optional[str]:
    """Find a skill whose folder was renamed upstream: first by folder name, then by SKILL.md `name:`."""
    want = sanitize_name(key)
    by_folder = [f for f in cache.skill_folders() if sanitize_name(posixpath.basename(f)) == want]
    if len(by_folder) == 1:
        return by_folder[0]
    by_name = [f for f, n in cache.skill_names().items() if n and sanitize_name(n) == want]
    return by_name[0] if len(by_name) == 1 else None


def _resolve(key: str, entry: Dict[str, Any], cache: Optional[RemoteCache], sync_error: Optional[str]) -> Union[Upstream, Item]:
    """The newest upstream folder for a lock entry, or an Item explaining why that cannot be known."""
    item = Item(name=key, kind=KIND_LOCK, status=UNKNOWN, source=short(str(entry.get("source") or "")))
    if cache is None:
        item.notes.append("source type %r is not supported" % entry.get("sourceType"))
        return item
    if sync_error:
        item.status = ERROR
        item.notes.append(sync_error)
        return item
    skill_path = entry.get("skillPath")
    if not isinstance(skill_path, str) or not skill_path:
        item.notes.append("the lock entry records no path inside the repository")
        return item
    folder = posixpath.dirname(skill_path)
    if not safe_folder(folder):
        item.notes.append("the lock file holds an unsafe skill path (%r); ignored" % folder)
        return item
    tree, moved = cache.tree(folder), False
    if tree is None:
        found = _relocate(cache, key)
        if found is not None:
            folder, tree, moved = found, cache.tree(found), True
    if tree is None:
        item.notes.append("%s no longer contains it (removed or renamed)" % (item.source or "the source"))
        return item
    listing, submodules = cache.listing(folder)
    if submodules:
        item.notes.append("contains git submodules, which skill-update cannot install")
        return item
    kept = filtered(listing)
    cli, unpredictable = cli_view(kept)
    return Upstream(key, entry, cache, folder, tree, kept, tree_sha(kept), tree_sha(cli),
                    unpredictable or len(cli) != len(kept), moved)


def _score(up: Upstream, tree: str, m: Manifest) -> float:
    """How strongly an installed folder belongs to this lock entry (used when several entries share a folder name)."""
    score = 0.0
    if tree == up.lock_hash:
        score += 4
    if tree in (up.cmp, up.cli_cmp):
        score += 3
    same = sum(1 for p, v in m.items() if up.kept.get(p) == v)
    score += same / max(1, len(up.kept) + len(m) - same)
    if sanitize_name(up.key) == up.key:
        score += 0.01  # an entry named exactly like the folder wins an otherwise equal tie
    return score


def check(ctx: Ctx, skills: List[Skill]) -> List[Item]:
    path = lock_path()
    if path is None:
        return []
    data = read_json(path)
    entries = data.get("skills") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        ctx.note("The npx skills lock file (%s) could not be read, so its skills are treated as unmanaged." % short(path))
        return []
    ctx.lock_path = path
    ctx.lock_writable = data.get("version") == LOCK_VERSION
    if not ctx.lock_writable:
        ctx.note("The npx skills lock file has format version %r (skill-update knows %d); it will not be modified."
                 % (data.get("version"), LOCK_VERSION))

    by_dir: Dict[str, List[Skill]] = {}
    for s in skills:
        if (s.real / ".git").exists():
            continue  # a git clone is updated with git (stash, fast-forward only), whatever the lock file says
        for e in s.exposures:
            bucket = by_dir.setdefault(sanitize_name(e.path.name), [])
            if s not in bucket:
                bucket.append(s)

    work: List[Tuple[str, Dict[str, Any], List[Skill]]] = []
    stale = 0
    for key in sorted(entries):
        entry, copies = entries[key], by_dir.get(sanitize_name(key), [])
        if not copies:
            stale += 1
        elif isinstance(entry, dict):
            work.append((key, entry, copies))
    if stale:
        ctx.note("%d npx skills lock entries have no installed folder and were ignored." % stale)

    caches: Dict[Tuple[Any, Any], RemoteCache] = {}
    for _key, entry, _copies in work:
        if _supported(entry):
            caches.setdefault(_cache_key(entry), RemoteCache(entry["sourceUrl"], entry.get("ref")))

    def sync(cache: RemoteCache) -> Optional[str]:
        try:
            cache.sync()
            return None
        except GitError as ex:
            return str(ex)

    errors = dict(zip(caches, ctx.parallel(sync, list(caches.values()))))
    resolved: Dict[str, Upstream] = {}
    unresolved: Dict[str, Item] = {}
    for key, entry, _copies in work:
        r = _resolve(key, entry, caches.get(_cache_key(entry)), errors.get(_cache_key(entry)))
        if isinstance(r, Upstream):
            resolved[key] = r
        else:
            unresolved[key] = r

    # One installed folder can match several lock entries (a repository may publish the same skill
    # twice, e.g. a Claude-specific and a generic copy). Give each folder to the entry it really came from.
    fingerprints: Dict[str, Tuple[Manifest, str]] = {}
    unreadable: Dict[str, str] = {}
    for _key, _entry, copies in work:
        for c in copies:
            real = str(c.real)
            if c.in_root and real not in fingerprints and real not in unreadable:
                try:
                    m = manifest(real)
                except OSError as ex:  # e.g. a file inside the skill that this user may not read
                    unreadable[real] = scrub(str(ex), 120)
                    continue
                fingerprints[real] = (m, tree_sha(m))
    claimants: Dict[str, List[str]] = {}
    for key, _entry, copies in work:
        for c in copies:
            claimants.setdefault(str(c.real), []).append(key)
    owner: Dict[str, str] = {}
    for real, keys in claimants.items():
        good = [k for k in keys if k in resolved]
        if real in fingerprints and len(good) > 1:
            m, t = fingerprints[real]
            owner[real] = max(good, key=lambda k: (_score(resolved[k], t, m), k))
        else:
            owner[real] = (good or keys)[0]

    items: List[Item] = []
    for key, entry, copies in work:
        mine = [c for c in copies if owner[str(c.real)] == key]
        for c in copies:
            ctx.claimed.add(str(c.real))
        if not mine:
            continue
        if key in resolved:
            items.append(_build(resolved[key], mine, fingerprints, unreadable))
        else:
            item = unresolved[key]
            item.agents = sorted({a for c in mine for a in c.agents})
            item.locations = sorted({short(c.real) for c in mine})
            items.append(item)
    return items


def _build(up: Upstream, copies: List[Skill], fingerprints: Dict[str, Tuple[Manifest, str]],
           unreadable: Dict[str, str]) -> Item:
    item = Item(name=up.key, kind=KIND_LOCK, status=CURRENT, source=short(str(up.entry.get("source") or "")),
                agents=sorted({a for c in copies for a in c.agents}),
                locations=sorted({short(c.real) for c in copies}),
                installed=up.lock_hash[:7], latest=up.tree[:7],
                compare_url=_compare_url(str(up.entry.get("sourceUrl")), up.folder))
    if up.moved:
        item.notes.append("moved upstream to %s/" % up.folder)

    states: List[Dict[str, Any]] = []
    for c in copies:
        if str(c.real) in unreadable:
            states.append({"skill": c, "state": "unreadable", "why": unreadable[str(c.real)]})
            continue
        if str(c.real) not in fingerprints:  # a symlink to a folder outside the skill folders
            states.append({"skill": c, "state": "external"})
            continue
        m, t = fingerprints[str(c.real)]
        if t in (up.cmp, up.cli_cmp):
            state, modified = "current", False   # identical to upstream, or to what the CLI installs from it
        elif up.tree == up.lock_hash:
            state, modified = "current", True    # upstream has not changed since install: the difference is a local edit
        elif t == up.lock_hash:
            state, modified = "outdated", False
        else:
            declared = frontmatter_name(c.real / "SKILL.md")
            if declared and sanitize_name(declared) != sanitize_name(up.key):
                state, modified = "unrelated", True
            else:
                # When the CLI leaves files out, an unedited install differs from upstream too, so an
                # edit cannot be told from the omission: do not claim the user changed anything.
                state, modified = "outdated", not up.cli_differs
        states.append({"skill": c, "manifest": m, "tree": t, "state": state, "modified": modified})

    outdated = [s for s in states if s["state"] == "outdated"]
    if outdated:
        item.status = OUTDATED
        item.modified = any(s["modified"] for s in outdated)
        item.changes = diff(outdated[0]["manifest"], up.kept)
    elif any(s["state"] in ("unrelated", "external", "unreadable") for s in states):
        item.status = BLOCKED
    for s in states:
        where = short(s["skill"].real)
        if s["state"] == "unreadable":
            item.notes.append("%s could not be read (%s); not touched" % (where, s["why"]))
        elif s["state"] == "external":
            item.notes.append("%s is a symlink to a folder outside your skill folders; not touched" % where)
        elif s["state"] == "unrelated":
            item.notes.append("%s has this name but different content (its SKILL.md says %r); not touched"
                              % (where, frontmatter_name(s["skill"].real / "SKILL.md")))
        elif s["state"] == "current" and s["modified"]:
            item.notes.append("%s has local edits; upstream has not changed" % where)

    clean = [s for s in states if "tree" in s]
    item.data = {
        "key": up.key, "cache": up.cache, "cache_key": _cache_key(up.entry), "folder": up.folder, "up_tree": up.tree,
        "up_cmp": up.cmp, "moved": up.moved, "copies": states,
        # the files are fine but the lock record is behind: refresh it so `npx skills check` agrees
        "heal": (item.status == CURRENT and up.lock_hash != up.tree and bool(clean)
                 and all(s["tree"] in (up.cmp, up.cli_cmp) for s in clean)),
    }
    return item


def apply(ctx: Ctx, run: Run, items: List[Item], heal: List[Item]) -> List[Result]:
    results: List[Result] = []
    updates: Dict[str, Dict[str, Any]] = {}
    groups: Dict[Tuple[Any, Any], List[Item]] = {}
    for it in items:
        groups.setdefault(it.data["cache_key"], []).append(it)
    for group in sorted(groups.values(), key=lambda g: any(i.name == SELF for i in g)):
        group.sort(key=lambda i: (i.name == SELF, i.name))
        cache: RemoteCache = group[0].data["cache"]
        folders = sorted({i.data["folder"] for i in group})
        try:
            try:
                cache.materialize(folders)  # one batched download per source
            except GitError:
                cache.rebuild()             # a killed git can leave a lock behind that makes the cache fail for good
                cache.materialize(folders)
        except GitError as ex:
            results += [Result(i.name, KIND_LOCK, "failed", "download failed: %s" % ex) for i in group]
            continue
        results += [_install(run, it, cache, updates) for it in group]
    for it in heal:
        updates.setdefault(it.data["key"], {})["skillFolderHash"] = it.data["up_tree"]
    if updates:
        _write_lock(ctx, run, updates, len(heal))
    return results


def _install(run: Run, it: Item, cache: RemoteCache, updates: Dict[str, Dict[str, Any]]) -> Result:
    d = it.data
    problems: List[str] = []
    replaced = 0
    for state in d["copies"]:
        if state["state"] != "outdated":
            continue
        skill: Skill = state["skill"]
        try:
            _replace(run, skill.real, cache.folder_path(d["folder"]), d["up_cmp"], it.name, state["tree"])
            replaced += 1
        except (OSError, shutil.Error, UpdateError) as ex:
            problems.append("%s: %s" % (short(skill.real), scrub(str(ex))))
    if problems:
        return Result(it.name, KIND_LOCK, "failed", "; ".join(problems))
    fields: Dict[str, Any] = {"skillFolderHash": d["up_tree"], "updatedAt": now_iso()}
    if d["moved"]:
        fields["skillPath"] = posixpath.join(d["folder"], "SKILL.md")
    updates[d["key"]] = fields
    detail = "%d folder%s replaced" % (replaced, "" if replaced == 1 else "s")
    if it.changes is not None:
        detail += " (%s files)" % it.changes.summary()
    return Result(it.name, KIND_LOCK, "updated", detail)


def _replace(run: Run, target: Path, source: Path, expected: str, label: str, before: str) -> None:
    """Swap one installed folder for the upstream version, verifying before and after."""
    if tree_sha(manifest(str(target))) != before:  # edited, or updated by something else, since the check
        raise UpdateError("the folder changed after it was checked; run skill-update again")
    staged = run.stage_dir(label)
    shutil.copytree(source, staged, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    if tree_sha(manifest(str(staged))) != expected:
        raise UpdateError("the downloaded files do not match the upstream hash")
    backup = run.swap_dir(target, staged, label)
    if tree_sha(manifest(str(target))) != expected:
        run.revert_swap(target, backup)
        raise UpdateError("the new folder failed verification, so the previous version was restored")
    carry_local_state(backup, target)  # only after verification: a revert must find the old folder complete


def _write_lock(ctx: Ctx, run: Run, updates: Dict[str, Dict[str, Any]], healed: int) -> None:
    if not ctx.lock_writable or ctx.lock_path is None:
        ctx.note("The npx skills lock file was left alone (unknown format); `npx skills check` may still list updated skills.")
        return
    data = read_json(ctx.lock_path)  # re-read: something else may have written to it since the check
    skills = data.get("skills") if isinstance(data, dict) else None
    if not isinstance(skills, dict):
        return
    run.snapshot_file(ctx.lock_path)
    before: Dict[str, Dict[str, Any]] = {}
    after: Dict[str, Dict[str, Any]] = {}
    for key, fields in updates.items():
        current = skills.get(key)
        if isinstance(current, dict):
            before[key] = {f: current.get(f) for f in fields}  # None where the field did not exist
            after[key] = dict(fields)
            current.update(fields)
    atomic_write(ctx.lock_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    run.record_lock(ctx.lock_path, before, after)
    if healed:
        ctx.note("Refreshed %d stale record%s in the npx skills lock file." % (healed, "" if healed == 1 else "s"))
