"""Backups, rollback and the single-run lock.

Every `update` run gets a folder under ~/.skill-update/backups/<run id>/ holding the folders it
replaced and a manifest.json describing everything it changed, which is what `rollback` reads.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .gitops import git
from .treehash import LOCAL_STATE_DIRS
from .util import atomic_write, first_error, read_json, state_dir

KEEP_RUNS = 10


class UpdateError(Exception):
    """A single skill could not be updated; the others carry on."""


def _move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, dst)
    except OSError:  # different file system
        shutil.move(str(src), str(dst))


def _plain_folders_down_to(base: Path, rel: Path) -> bool:
    """True when every folder from `base` to the parent of `rel` really is a folder, not a symlink.
    The new folder comes from upstream and may contain a symlink such as `scripts -> ~/.ssh`; following
    it would move our files outside the skill."""
    cur = base
    for part in rel.parts[:-1]:
        cur = cur / part
        try:
            if not stat.S_ISDIR(os.lstat(cur).st_mode):
                return False
        except OSError:
            return False
    return True


def carry_local_state(old: Path, new: Path) -> None:
    """Move node_modules / virtualenvs from the replaced folder into the new one, so no reinstall is needed."""
    for root, dirs, _files in os.walk(old):
        for d in list(dirs):
            if d not in LOCAL_STATE_DIRS:
                continue
            dirs.remove(d)
            rel = Path(os.path.relpath(os.path.join(root, d), old))
            dst = new / rel
            if os.path.lexists(dst) or not _plain_folders_down_to(new, rel):
                continue
            try:
                _move(Path(root) / d, dst)
            except OSError:
                pass  # it stays in the backup


class Run:
    """One `update` invocation: its backup folder, staging area and rollback manifest."""

    def __init__(self, keep_backup: bool = True) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        base = state_dir() / "backups"
        self.id, n = stamp, 1
        while (base / self.id).exists():
            n += 1
            self.id = "%s-%d" % (stamp, n)
        self.dir = base / self.id
        self.staging = state_dir() / "staging" / self.id
        self.keep_backup = keep_backup
        self.entries: List[Dict[str, Any]] = []
        self._counter = 0

    def _next(self, label: str) -> str:
        self._counter += 1
        return "%03d-%s" % (self._counter, label)

    def save(self) -> None:
        atomic_write(self.dir / "manifest.json", json.dumps(
            {"id": self.id, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "entries": self.entries},
            indent=2) + "\n")

    def stage_dir(self, label: str) -> Path:
        """A path (not yet created) where the new version of a skill is assembled before it replaces the old one."""
        self.staging.mkdir(parents=True, exist_ok=True)
        return self.staging / self._next(label)

    def swap_dir(self, target: Path, new: Path, label: str) -> Path:
        """Put `new` where `target` is. The old folder moves into the backup area; its new location is returned."""
        backup = self.dir / "dirs" / self._next(label)
        entry = {"kind": "dir", "target": str(target), "backup": str(backup), "label": label, "state": "pending"}
        self.entries.append(entry)
        self.save()
        try:
            _move(target, backup)
        except BaseException:  # nothing was moved (for example the folder is read-only): forget the entry
            self.entries.remove(entry)
            self.save()
            raise
        try:
            _move(new, target)
        except BaseException:
            _move(backup, target)  # put the original back before reporting the failure
            self.entries.remove(entry)
            self.save()
            raise
        entry["state"] = "done"
        self.save()
        return backup

    def revert_swap(self, target: Path, backup: Path) -> None:
        """Undo a swap whose result failed verification."""
        shutil.rmtree(target, ignore_errors=True)
        _move(backup, target)
        for e in self.entries:
            if e.get("kind") == "dir" and e.get("target") == str(target) and e.get("state") == "done":
                e["state"] = "reverted"
        self.save()

    def backup_file(self, path: Path) -> None:
        dst = self.dir / "files" / self._next(path.name)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dst)
        self.entries.append({"kind": "file", "target": str(path), "backup": str(dst), "state": "done"})
        self.save()

    def record_git(self, repo: Path, old: str, new: str, stash: Optional[str], label: str) -> None:
        self.entries.append({"kind": "git", "target": str(repo), "old": old, "new": new, "stash": stash,
                             "label": label, "state": "done"})
        self.save()

    def finish(self) -> None:
        shutil.rmtree(self.staging, ignore_errors=True)
        if not self.keep_backup:  # --no-backup: the replaced folders are deleted once everything is verified
            for e in self.entries:
                if e.get("state") == "done" and e.get("kind") in ("dir", "file"):
                    _discard(Path(e["backup"]))
                    e["state"] = "discarded"
            if self.entries:
                self.save()
        if not self.entries:
            shutil.rmtree(self.dir, ignore_errors=True)
        _prune()


def _discard(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            os.unlink(path)
        except OSError:
            pass


def _prune() -> None:
    base = state_dir() / "backups"
    if not base.is_dir():
        return
    runs = sorted(p for p in base.iterdir() if p.is_dir())
    for old in runs[:-KEEP_RUNS]:
        shutil.rmtree(old, ignore_errors=True)


def list_runs() -> List[Dict[str, Any]]:
    base = state_dir() / "backups"
    out = []
    for p in sorted(base.iterdir()) if base.is_dir() else []:
        data = read_json(p / "manifest.json")
        if isinstance(data, dict):
            entries = data.get("entries", [])
            out.append({"id": data.get("id", p.name), "created": data.get("created", ""),
                        "changes": len([e for e in entries if e.get("state") in ("done", "pending", "restored")]),
                        "restorable": len([e for e in entries if e.get("state") in ("done", "pending")])})
    return out


def rollback(run_id: Optional[str]) -> List[str]:
    """Restore everything a run changed. Returns human-readable lines describing what happened."""
    runs = list_runs()
    if not runs:
        raise UpdateError("there are no backups to restore")
    wanted = run_id or runs[-1]["id"]
    run_dir = state_dir() / "backups" / wanted
    data = read_json(run_dir / "manifest.json")
    if not isinstance(data, dict):
        raise UpdateError("no backup named %s (see: rollback --list)" % wanted)
    lines: List[str] = []
    for e in reversed(data.get("entries", [])):
        if e.get("state") not in ("done", "pending"):  # "pending": the run stopped halfway through a swap
            continue
        kind, target = e.get("kind"), Path(e.get("target", ""))
        label = e.get("label") or target.name
        try:
            if kind == "dir":
                backup = Path(e["backup"])
                if not backup.is_dir():
                    if e.get("state") == "done":
                        lines.append("! %s: the backup folder is gone" % label)
                    continue  # pending and no backup: nothing had been moved yet
                if target.exists() or target.is_symlink():
                    aside = run_dir / "rolled-back" / ("%s-%d" % (label, int(time.time())))
                    _move(target, aside)
                    _move(backup, target)
                    carry_local_state(aside, target)  # node_modules etc. go back with the old folder
                else:
                    _move(backup, target)
            elif kind == "file":
                shutil.copy2(e["backup"], target)
            elif kind == "git":
                head = git("rev-parse", "HEAD", cwd=target)
                if head.out.strip() != e.get("new"):
                    lines.append("! %s: the repository has moved on since the update; left as it is" % label)
                    continue
                r = git("reset", "--hard", "-q", e["old"], cwd=target)
                if not r.ok:
                    raise UpdateError(first_error(r))
                if e.get("stash"):
                    r = git("stash", "apply", "-q", e["stash"], cwd=target)
                    if not r.ok:
                        lines.append("! %s: restored the old version, but your saved edits could not be re-applied "
                                     "(they are in `git stash`)" % label)
            else:
                continue
        except (OSError, shutil.Error, UpdateError) as ex:
            lines.append("! %s: %s" % (label, ex))
            continue
        e["state"] = "restored"
        lines.append("restored %s" % label)
    atomic_write(run_dir / "manifest.json", json.dumps(data, indent=2) + "\n")
    return lines or ["nothing to restore in %s" % wanted]


class RunLock:
    """Only one skill-update run may change things at a time."""

    def __init__(self) -> None:
        self._fh = None

    def __enter__(self) -> "RunLock":
        try:
            import fcntl
        except ImportError:  # no flock on this platform; carry on unprotected
            return self
        path = state_dir() / "run.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(path, "w")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            raise UpdateError("another skill-update run is in progress")
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._fh is not None:
            self._fh.close()
