"""Skills that are git clones sitting in a skills folder (for example a skill pack cloned by hand).

Only clones that live directly in a skill folder are touched. A skill that is a symlink into some
other repository, or a sub-folder of a bigger repository, is somebody's working copy: it is
reported by the scanner as unmanaged and never fetched or pulled.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Optional

from .backup import Run
from .context import Ctx
from .discover import Skill
from .gitops import git
from .model import BLOCKED, CURRENT, ERROR, KIND_GIT, OUTDATED, UNKNOWN, Changes, Item, Result
from .util import first_error, scrub, short

SELF = "skill-update"
SETUP_FILES = ("setup", "setup.sh", "install.sh", "bootstrap.sh")


# Settings that make git run a program of the repository's choosing. A skill folder that ships its own
# .git/config with one of these is never fetched into: the user installed a skill, not permission to
# run code every time a maintenance tool looks at it.
_RISKY_CONFIG = re.compile(
    r"^(core\.(fsmonitor|sshcommand|hookspath|pager|editor|askpass|gitproxy)|diff\.external|gpg\.program"
    r"|credential\.(.+\.)?helper|(filter|merge|diff)\..+\.(clean|smudge|process|driver|command|textconv)"
    r"|remote\..+\.(uploadpack|receivepack|vcs|proxy)|include\.path|includeif\..+\.path|http\.proxy)$", re.I)


def _unsafe_reason(repo: Path) -> Optional[str]:
    """Why git should not be run in this folder, or None when it looks like an ordinary clone."""
    dot_git = repo / ".git"
    if not dot_git.is_dir() or dot_git.is_symlink():
        return "its .git is not a plain folder (a worktree, submodule or link)"
    if hasattr(os, "geteuid") and dot_git.stat().st_uid != os.geteuid():
        return "its .git belongs to another user"
    config = git("config", "--local", "--list", "-z", cwd=repo)
    for entry in config.out.split("\0"):
        key = entry.split("\n", 1)[0].strip()
        if key and _RISKY_CONFIG.match(key):
            return "its .git/config sets %s, which can run programs" % key
    return None


def check(ctx: Ctx, skills: List[Skill]) -> List[Item]:
    repos = [s for s in skills if str(s.real) not in ctx.claimed and s.in_root and (s.real / ".git").exists()]
    found = ctx.parallel(lambda s: _check_repo(ctx, s), repos)
    return [it for it in found if it is not None]


def _source(url: str) -> str:
    m = re.search(r"github\.com[:/]+([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else scrub(url, 60)


def _count(repo: Path, *args: str) -> str:
    return git(*args, cwd=repo).out.strip()


def _check_repo(ctx: Ctx, s: Skill) -> Optional[Item]:
    repo = s.real
    unsafe = _unsafe_reason(repo)
    if unsafe:
        ctx.claimed.add(str(repo))
        return Item(name=s.name, kind=KIND_GIT, status=BLOCKED, agents=s.agents, locations=[short(repo)],
                    notes=["not touched: %s" % unsafe])
    if not git("remote", cwd=repo).out.split():
        ctx.unmanaged_notes[str(repo)] = ["git repository without a remote"]
        return None
    ctx.claimed.add(str(repo))
    item = Item(name=s.name, kind=KIND_GIT, status=CURRENT, agents=s.agents, locations=[short(repo)])

    branch = git("symbolic-ref", "-q", "--short", "HEAD", cwd=repo)
    upstream = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", cwd=repo)
    if not branch.ok or not upstream.ok:
        item.status = UNKNOWN
        item.notes.append("detached HEAD" if not branch.ok else "branch %s tracks no remote branch" % branch.out.strip())
        return item
    remote = upstream.out.strip().split("/")[0]
    item.source = _source(_count(repo, "config", "--get", "remote.%s.url" % remote))

    fetched = git("fetch", "--quiet", "--no-tags", remote, cwd=repo, timeout=300)
    if not fetched.ok:
        item.status = ERROR
        item.notes.append(first_error(fetched))
        return item

    ahead, behind = (int(n) for n in _count(repo, "rev-list", "--left-right", "--count", "HEAD...@{u}").split())
    dirty = bool(_count(repo, "status", "--porcelain"))
    item.installed, item.latest = _count(repo, "rev-parse", "--short", "HEAD"), _count(repo, "rev-parse", "--short", "@{u}")
    item.modified = dirty
    item.data = {"repo": str(repo), "behind": behind, "ahead": ahead, "dirty": dirty}

    if behind == 0:
        if ahead:
            item.notes.append("%d local commit%s not on the remote" % (ahead, "" if ahead == 1 else "s"))
        if dirty:
            item.notes.append("uncommitted local changes")
        return item
    if ahead:
        item.status = BLOCKED
        item.notes.append("diverged (%d local, %d upstream commit%s): merge it yourself"
                          % (ahead, behind, "" if behind == 1 else "s"))
        return item

    item.status = OUTDATED
    item.commits = [ln for ln in _count(repo, "log", "--format=%s", "-n", "10", "HEAD..@{u}").splitlines() if ln]
    item.changes = _changes(repo)
    incoming = set(item.changes.added) | set(item.changes.modified)
    if any((repo / f).is_file() or f in incoming for f in SETUP_FILES):
        item.notes.append("has a setup script; skill-update never runs it")
    return item


def _changes(repo: Path) -> Changes:
    ch = Changes()
    parts = git("diff", "--name-status", "--no-renames", "-z", "HEAD", "@{u}", cwd=repo).out.split("\0")
    for status, path in zip(parts[0::2], parts[1::2]):
        {"A": ch.added, "D": ch.removed}.get(status, ch.modified).append(path)
    return ch


def apply(ctx: Ctx, run: Run, items: List[Item]) -> List[Result]:
    return [_pull(run, it) for it in sorted(items, key=lambda i: (i.name == SELF, i.name))]


def _pull(run: Run, it: Item) -> Result:
    repo = Path(it.data["repo"])
    old = _count(repo, "rev-parse", "HEAD")
    stash: Optional[str] = None
    if it.data["dirty"]:  # keep the user's edits safe in a stash instead of overwriting them
        before = _count(repo, "rev-parse", "-q", "--verify", "refs/stash")
        r = git("stash", "push", "--include-untracked", "-m", "skill-update %s" % run.id, cwd=repo)
        if not r.ok:
            return Result(it.name, KIND_GIT, "failed", "could not set your local edits aside: %s" % first_error(r))
        after = _count(repo, "rev-parse", "-q", "--verify", "refs/stash")
        stash = after if after and after != before else None
    merged = git("merge", "--ff-only", "-q", "@{u}", cwd=repo, timeout=300)
    if not merged.ok:
        if stash:
            git("stash", "pop", "-q", cwd=repo)
        return Result(it.name, KIND_GIT, "failed", first_error(merged))
    new = _count(repo, "rev-parse", "HEAD")
    run.record_git(repo, old, new, stash, it.name)
    notes = []
    if stash:
        notes.append("your local edits are saved in `git stash` (\"skill-update %s\")" % run.id)
    if any((repo / f).is_file() for f in SETUP_FILES):
        notes.append("has a setup script: run it yourself if its README says so")
    n = it.data["behind"]
    return Result(it.name, KIND_GIT, "updated", "%s → %s (%d commit%s)" % (old[:7], new[:7], n, "" if n == 1 else "s"), notes)
