"""Command line: check, update, rollback, scan."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, List, Optional, Sequence

from . import __version__, backup, discover, gitskill, lockfile, plugins, policy, report
from .context import Ctx
from .gitops import git_version
from .model import KIND_UNMANAGED, OUTDATED, UNMANAGED, Item
from .util import read_json, scrub, short, state_dir

CHANNELS = ("lock", "git", "plugin")
MIN_GIT = (2, 30)  # partial clones and cone-mode sparse checkouts


class Collected:
    def __init__(self, items: List[Item], scan: discover.Scan, elapsed: float) -> None:
        self.items, self.scan, self.elapsed = items, scan, elapsed


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print machine-readable JSON (for agents and scripts)")
    common.add_argument("-v", "--verbose", action="store_true", help="also list up-to-date and unmanaged skills")
    common.add_argument("--root", action="append", default=[], metavar="DIR", help="also scan this skills folder (repeatable)")
    common.add_argument("--only", action="append", default=[], choices=CHANNELS, help="limit to one install channel (repeatable)")
    common.add_argument("--ignore", action="append", default=[], metavar="NAME", help="leave this skill out (repeatable)")
    common.add_argument("--no-color", action="store_true")

    p = argparse.ArgumentParser(
        prog="skill_update.py",
        description="Check and update every agent skill installed on this machine: npx skills, git clones and Claude Code plugins.")
    p.add_argument("--version", action="version", version="skill-update " + __version__)
    sub = p.add_subparsers(dest="command", metavar="{check,update,rollback,scan}")

    c = sub.add_parser("check", parents=[common], help="show what has a newer version (changes nothing; default)")
    c.add_argument("names", nargs="*", help="only these skills, or every skill from one source (owner/repo)")
    c.add_argument("--refresh", action="store_true", help="refresh the Claude Code plugin catalogs first")
    c.add_argument("--exit-code", action="store_true", help="exit with 1 when updates are available (for CI)")

    u = sub.add_parser("update", parents=[common], help="update outdated skills, backing them up first")
    u.add_argument("names", nargs="*", help="only these skills, or every skill from one source (owner/repo)")
    u.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    u.add_argument("--dry-run", action="store_true", help="show the plan and change nothing")
    u.add_argument("--keep-modified", action="store_true", help="leave skills you changed locally alone")
    u.add_argument("--no-backup", action="store_true", help="do not keep the replaced folders")
    u.add_argument("--refresh", action="store_true", help="refresh the Claude Code plugin catalogs before planning")

    r = sub.add_parser("rollback", help="restore what an update run replaced")
    r.add_argument("run_id", nargs="?", help="the run to undo (default: the latest)")
    r.add_argument("--list", action="store_true", help="list the runs that can be restored")
    r.add_argument("--json", action="store_true")

    sub.add_parser("scan", parents=[common], help="list the skill folders and skills that were found (no network)")
    return p


def _ctx(args: argparse.Namespace) -> Ctx:
    cfg = read_json(state_dir() / "config.json")
    cfg = cfg if isinstance(cfg, dict) else {}
    args.ignore = set(args.ignore) | set(cfg.get("ignore") or [])
    args.root = list(args.root) + list(cfg.get("roots") or [])
    return Ctx(args)


def collect(ctx: Ctx, refresh_plugins: bool) -> Collected:
    started = time.time()
    only = set(ctx.opts.only)
    sc = discover.scan(ctx.opts.root)
    items: List[Item] = []
    if not only or "lock" in only:
        items += lockfile.check(ctx, sc.skills)
    if not only or "git" in only:
        items += gitskill.check(ctx, sc.skills)
    if not only or "plugin" in only:
        items += plugins.check(ctx, refresh_plugins)
    if not only:
        for s in sc.skills:
            if str(s.real) not in ctx.claimed:
                items.append(Item(name=s.name, kind=KIND_UNMANAGED, status=UNMANAGED, agents=s.agents,
                                  locations=[short(s.real)],
                                  notes=ctx.unmanaged_notes.get(str(s.real)) or discover.explain_unmanaged(s)))
    ignored = [i for i in items if i.name in ctx.opts.ignore]
    if ignored:
        ctx.note("Left out %d item%s listed in --ignore or config.json." % (len(ignored), "" if len(ignored) == 1 else "s"))
    items = [i for i in items if i.name not in ctx.opts.ignore]
    for i in items:
        i.sanitize()  # names and notes can come from upstream repositories: never print their control characters
    return Collected(items, sc, time.time() - started)


def _only_named(items: Sequence[Item], names: Sequence[str]) -> List[Item]:
    return [i for i in items if policy.matches(i, names)] if names else list(items)


def _dump(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_check(args: argparse.Namespace) -> int:
    ctx = _ctx(args)
    col = collect(ctx, refresh_plugins=args.refresh)
    items = _only_named(col.items, args.names)
    if args.json:
        _dump(report.check_json(items, col.scan.roots, ctx.notes, col.elapsed))
    else:
        print(report.render_check(items, len(col.scan.roots), ctx.notes, col.elapsed, args.verbose,
                                  report.make_style(args.no_color)), end="")
    return 1 if args.exit_code and any(i.status == OUTDATED for i in items) else 0


def cmd_scan(args: argparse.Namespace) -> int:
    ctx = _ctx(args)
    sc = discover.scan(ctx.opts.root)
    if args.json:
        _dump({"skill_folders": [{"agent": a, "path": short(p), "skills": n} for a, p, n in sc.roots],
               "skills": [{"name": s.name, "location": short(s.real), "agents": s.agents, "inside_skill_folder": s.in_root}
                          for s in sc.skills]})
    else:
        print(report.render_scan(sc.roots, sc.skills, sc.containers), end="")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    ctx = _ctx(args)
    style = report.make_style(args.no_color)
    # Refreshing the plugin catalogs writes into Claude Code's folders, so it happens only when asked for
    # (--refresh) or inside the plugin update itself, after the user has agreed.
    col = collect(ctx, refresh_plugins=args.refresh and not args.dry_run)
    chosen, skipped = policy.select(col.items, args)
    heal = [] if args.names else [i for i in col.items if i.data.get("heal")]
    edited = sorted(i.name for i in chosen if i.modified)
    if args.no_backup and edited:
        print("skill-update: not using --no-backup while %d skill%s local edits (%s). Drop --no-backup, or add --keep-modified."
              % (len(edited), " has" if len(edited) == 1 else "s have", ", ".join(edited[:5]) + (", …" if len(edited) > 5 else "")),
              file=sys.stderr)
        return 2

    if not chosen and not heal:
        if args.json:
            _dump(report.update_json([], "", skipped, ctx.notes, False))
        else:
            print("Everything is up to date." if not skipped else report.render_plan([], skipped, style), end="")
        return 0

    if not args.json:
        print(report.render_plan(chosen, skipped, style), end="")
    if heal and not chosen and not args.json:
        print("No skill needs updating, but %d npx skills lock record%s out of date (the files are fine) and will be refreshed."
              % (len(heal), " is" if len(heal) == 1 else "s are"))
    if args.dry_run:
        if args.json:
            _dump(dict(report.update_json([], "", skipped, ctx.notes, False), planned=[i.to_dict() for i in chosen]))
        else:
            print("\nDry run: nothing was changed.")
        return 0
    if not args.yes:  # anything that writes needs a yes, including refreshing stale lock records
        question = ("Apply these %d update%s?" % (len(chosen), "" if len(chosen) == 1 else "s")) if chosen else \
                   ("Refresh %d stale lock record%s?" % (len(heal), "" if len(heal) == 1 else "s"))
        if sys.stdin.isatty() and sys.stdout.isatty() and not args.json:
            if input("\n%s [y/N] " % question).strip().lower() not in ("y", "yes"):
                print("Cancelled.")
                return 0
        else:
            print("skill-update: not changing anything without --yes (there is no terminal to ask on).", file=sys.stderr)
            return 2

    with backup.RunLock():
        run = backup.Run(keep_backup=not args.no_backup)
        results = []
        finished = False
        try:
            results += lockfile.apply(ctx, run, [i for i in chosen if i.kind == "lock"], heal)
            results += gitskill.apply(ctx, run, [i for i in chosen if i.kind == "git"])
            results += plugins.apply(ctx, run, [i for i in chosen if i.kind == "plugin"])
            finished = True
        except BaseException:
            if run.entries:  # something was already replaced: say how to undo it before the error is shown
                print("skill-update: stopped after changing %d item%s. Undo with: python3 skill_update.py rollback %s"
                      % (len(run.entries), "" if len(run.entries) == 1 else "s", run.id), file=sys.stderr)
            raise
        finally:
            run.finish(discard=finished)  # an error or interruption keeps every backup, even with --no-backup
    for r in results:
        r.sanitize()

    backed_up = not args.no_backup
    if args.json:
        _dump(report.update_json(results, run.id, skipped, ctx.notes, backed_up))
    else:
        print("\n" + report.render_results(results, run.id, backed_up, ctx.notes, style), end="")
    return 1 if any(r.outcome == "failed" for r in results) else 0


def cmd_rollback(args: argparse.Namespace) -> int:
    if args.list:
        runs = backup.list_runs()
        if args.json:
            _dump({"runs": runs})
        else:
            print("\n".join("%s  %s  %d change%s" % (r["id"], r["created"], r["restorable"], "" if r["restorable"] == 1 else "s")
                            for r in runs) or "No backups yet.")
        return 0
    with backup.RunLock():
        lines = backup.rollback(args.run_id)
    if args.json:
        _dump({"restored": lines})
    else:
        print("\n".join(lines))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except (OSError, ValueError):
            pass
    args_list = list(sys.argv[1:] if argv is None else argv)
    if not args_list or (args_list[0].startswith("-") and args_list[0] not in ("-h", "--help", "--version")):
        args_list.insert(0, "check")  # `skill_update.py` and `skill_update.py -v` mean `check`
    parser = build_parser()
    args = parser.parse_args(args_list)
    if args.command is None:
        parser.print_help()
        return 2
    version = git_version()
    if version is None or version < MIN_GIT:
        print("skill-update: git %s or newer is required (found %s)."
              % (".".join(map(str, MIN_GIT)), ".".join(map(str, version)) if version else "none"), file=sys.stderr)
        return 2
    handler = {"check": cmd_check, "update": cmd_update, "rollback": cmd_rollback, "scan": cmd_scan}[args.command]
    try:
        return handler(args)
    except backup.UpdateError as ex:
        print("skill-update: %s" % ex, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as ex:  # a bug, or something odd on this machine: short message, traceback on request
        if os.environ.get("SKILL_UPDATE_DEBUG"):
            raise
        print("skill-update: unexpected error (%s): %s\nSet SKILL_UPDATE_DEBUG=1 for the traceback and please report it: "
              "https://github.com/kjhk3082/skill-update/issues" % (type(ex).__name__, scrub(str(ex))), file=sys.stderr)
        return 2
