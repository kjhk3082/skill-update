"""Turn items and results into text for people, or JSON for agents."""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Sequence, Tuple

from . import __version__
from .model import (BLOCKED, CURRENT, ERROR, KIND_GIT, KIND_LOCK, KIND_PLUGIN, OUTDATED, STATUS_ORDER,
                    UNKNOWN, UNMANAGED, Item, Result)
from .util import short


class Style:
    """Optional ANSI colors: off when piped, when NO_COLOR is set, or with --no-color."""
    _CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "cyan": "36"}

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, kind: str, text: str) -> str:
        return "\033[%sm%s\033[0m" % (self._CODES[kind], text) if self.enabled else text


def make_style(no_color: bool) -> Style:
    """Color on a terminal; FORCE_COLOR=1 turns it on for pipes (used to render the README screenshots)."""
    wanted = sys.stdout.isatty() or bool(os.environ.get("FORCE_COLOR"))
    return Style(wanted and not no_color and "NO_COLOR" not in os.environ)


_GROUPS = {
    OUTDATED: ("↑", "Updates available", "yellow"),
    BLOCKED: ("!", "Needs your attention", "red"),
    ERROR: ("✗", "Could not check", "red"),
    UNKNOWN: ("?", "Cannot tell", "cyan"),
    CURRENT: ("✓", "Up to date", "green"),
    UNMANAGED: ("·", "No upstream known", "dim"),
}


def _plural(n: int, one: str, many: str) -> str:
    return "%d %s" % (n, one if n == 1 else many)


def _where(it: Item) -> str:
    label = {KIND_LOCK: "npx skills", KIND_GIT: "git", KIND_PLUGIN: "plugin"}.get(it.kind, "")
    return " · ".join(x for x in (label, it.source) if x)


def _detail(it: Item) -> str:
    bits: List[str] = []
    if it.status == OUTDATED:
        if it.changes is not None:
            bits.append("%s files" % it.changes.summary())
        elif it.installed and it.latest:
            bits.append("%s → %s" % (it.installed, it.latest))
        if it.data.get("behind"):
            bits.append("%d commit%s" % (it.data["behind"], "" if it.data["behind"] == 1 else "s"))
        if it.modified:
            bits.append("edited locally · backed up first")
    elif it.kind == KIND_PLUGIN and it.status == UNKNOWN:
        bits.append("Claude Code decides")
    return " · ".join(bits)


def _label(it: Item, rows: Sequence[Item]) -> str:
    """The skill name, plus its folder when the same name is listed more than once."""
    twins = sum(1 for r in rows if r.name == it.name)
    return "%s [%s]" % (it.name, it.locations[0]) if twins > 1 and it.locations else it.name


def _table(rows: Sequence[Item], style: Style, notes: bool) -> List[str]:
    if not rows:
        return []
    labels = [_label(r, rows) for r in rows]
    name_w = min(52, max(len(x) for x in labels))
    where_w = min(46, max(len(_where(r)) for r in rows))
    lines: List[str] = []
    for r, label in zip(rows, labels):
        lines.append("   %s  %s  %s" % (label[:name_w].ljust(name_w), style("dim", _where(r)[:where_w].ljust(where_w)),
                                        _detail(r)))
        if notes:
            lines += [style("dim", "      ↳ " + n) for n in r.notes]
    return lines


def _counts(items: Sequence[Item]) -> Dict[str, int]:
    counts = {s: 0 for s in STATUS_ORDER}
    for it in items:
        counts[it.status] += 1
    return counts


def render_check(items: Sequence[Item], folders: int, notes: Sequence[str], elapsed: float,
                 verbose: bool, style: Style) -> str:
    groups: Dict[str, List[Item]] = {s: [] for s in STATUS_ORDER}
    for it in sorted(items, key=lambda i: (i.name.lower(), i.kind)):
        groups[it.status].append(it)
    out = [style("bold", "skill-update %s" % __version__)
           + style("dim", "  %s from %s in %.1fs" % (_plural(len(items), "skill or plugin", "skills and plugins"),
                                                    _plural(folders, "skill folder", "skill folders"), elapsed))]
    for status in STATUS_ORDER:
        rows = groups[status]
        if not rows:
            continue
        icon, title, color = _GROUPS[status]
        out += ["", style(color, "%s %s (%d)" % (icon, title, len(rows)))]
        if status == UNKNOWN and not verbose:
            # plugins that publish no version are one fact, not dozens of lines
            plugins_unknown = [r for r in rows if r.kind == KIND_PLUGIN]
            rows = [r for r in rows if r.kind != KIND_PLUGIN]
            if rows:
                out += _table(rows, style, notes=True)
            if plugins_unknown:
                one = len(plugins_unknown) == 1
                out.append(style("dim", "   %d plugin%s %s no catalog version; Claude Code decides (--verbose lists them)"
                                 % (len(plugins_unknown), "" if one else "s", "has" if one else "have")))
        elif status in (OUTDATED, BLOCKED, ERROR) or verbose:
            out += _table(rows, style, notes=status != CURRENT or verbose)
        else:
            out.append(style("dim", "   list them with --verbose"))
    pending = len(groups[OUTDATED]) + len([i for i in groups[UNKNOWN] if i.kind == KIND_PLUGIN])
    if pending:
        out += ["", "Apply with:  python3 skill_update.py update    (everything is backed up first)"]
    elif not items:
        out += ["", "No skills found. Skill folders are looked up in the places listed by: python3 skill_update.py scan"]
    out += [""] + [style("dim", "note: " + n) for n in notes]
    return "\n".join(out).rstrip() + "\n"


def render_plan(chosen: Sequence[Item], skipped: Sequence[Tuple[Item, str]], style: Style) -> str:
    out: List[str] = []
    if chosen:
        out += [style("bold", "Will update %d:" % len(chosen))]
        out += _table(sorted(chosen, key=lambda i: (i.name.lower(), i.kind)), style, notes=True)
    if skipped:
        rows = [it for it, _ in skipped]  # the same name can be installed in two folders
        out += ([""] if out else []) + [style("dim", "Skipping:")] + [style("dim", "   %s — %s" % (_label(it, rows), why)) for it, why in skipped]
    return "\n".join(out) + "\n" if out else ""


_MARKS = {"updated": ("✓", "green"), "unchanged": ("=", "dim"), "failed": ("✗", "red"), "skipped": ("–", "dim")}


def render_results(results: Sequence[Result], run_id: str, backed_up: bool, notes: Sequence[str], style: Style) -> str:
    out: List[str] = []
    for r in sorted(results, key=lambda r: (r.outcome != "failed", r.name.lower())):
        mark, color = _MARKS.get(r.outcome, ("?", "dim"))
        out.append("%s %s  %s" % (style(color, mark), r.name, style("dim", r.detail)))
        out += [style("dim", "    ↳ " + n) for n in r.notes]
    n = {o: len([r for r in results if r.outcome == o]) for o in _MARKS}
    out += ["", "%d updated, %d unchanged, %d failed." % (n["updated"], n["unchanged"], n["failed"])]
    if backed_up and n["updated"]:
        out.append("Undo with:  python3 skill_update.py rollback %s" % run_id)
    out += [style("dim", "note: " + x) for x in notes]
    return "\n".join(out) + "\n"


def render_scan(folders: Sequence[Tuple[str, Any, int]], skills: Sequence[Any], containers: int) -> str:
    out = ["Skill folders found:"]
    out += ["   %-16s %-44s %d skill%s" % (agent, short(path), n, "" if n == 1 else "s") for agent, path, n in folders]
    if not folders:
        out.append("   (none)")
    out += ["", "%d distinct skills" % len(skills) + (" (%d category folders without a SKILL.md were skipped)" % containers if containers else "")]
    return "\n".join(out) + "\n"


def check_json(items: Sequence[Item], folders: Sequence[Tuple[str, Any, int]], notes: Sequence[str], elapsed: float) -> Dict[str, Any]:
    return {
        "tool": "skill-update", "version": __version__, "command": "check", "elapsed_seconds": round(elapsed, 1),
        "skill_folders": [{"agent": a, "path": short(p), "skills": n} for a, p, n in folders],
        "summary": dict(_counts(items), total=len(items)),
        "items": [it.to_dict() for it in sorted(items, key=lambda i: (i.name.lower(), i.kind))],
        "notes": list(notes),
    }


def update_json(results: Sequence[Result], run_id: str, skipped: Sequence[Tuple[Item, str]],
                notes: Sequence[str], backed_up: bool) -> Dict[str, Any]:
    n = {o: len([r for r in results if r.outcome == o]) for o in _MARKS}
    return {
        "tool": "skill-update", "version": __version__, "command": "update",
        "run_id": run_id if backed_up and n["updated"] else None,
        "summary": n,
        "results": [r.to_dict() for r in results],
        "skipped": [{"name": it.name, "reason": why} for it, why in skipped],
        "notes": list(notes),
    }
