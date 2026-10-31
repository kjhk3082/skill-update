"""Backups, rollback and the single-run lock.

Every `update` run gets a folder under ~/.skill-update/backups/<run id>/ holding the folders it
replaced and a manifest.json describing everything it changed, which is what `rollback` reads.

Folders are only ever moved with rename(2). A rename is atomic, so a crash leaves the old folder or
the new one, never half of each, and nothing is ever deleted while it is the only copy. That needs the
skill folder, the staging area and the backups to sit on one disk; when they do not, the skill is
skipped with an explanation instead of being copied.
"""
from __future__ import annotations

import errno
import json
import os
import re
import shutil
import stat
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .gitops import git
from .treehash import LOCAL_STATE_DIRS
from .util import atomic_write, first_error, read_json, short, state_dir

KEEP_RUNS = 10


class UpdateError(Exception):
    """A single skill could not be updated; the others carry on."""


def _slug(label: str) -> str:
    """A label that is safe as one path component: lock keys come from third parties and may hold `/` or `..`."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", label)[:48].strip("._") or "skill"


def _inside(base: Path, path: Path) -> bool:
    b, p = os.path.realpath(base), os.path.realpath(path)
    return p == b or p.startswith(b + os.sep)


def _device(path: Path) -> int:
    while not path.exists() and path != path.parent:
        path = path.parent
    return os.stat(path).st_dev


def _move(src: Path, dst: Path) -> None:
    """Rename src to dst. Never falls back to copy-and-delete, which is not atomic."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, dst)
    except OSError as ex:
        if ex.errno == errno.EXDEV:
            raise UpdateError("%s and %s are on different disks; skill-update only moves folders within one disk. "
                              "Set SKILL_UPDATE_HOME to a folder on the disk that holds your skills." % (short(src), short(dst))) from ex
        raise


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
            except (OSError, UpdateError):
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
        return "%03d-%s" % (self._counter, _slug(label))

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
        self.staging.mkdir(parents=True, exist_ok=True)
        self.dir.mkdir(parents=True, exist_ok=True)
        if _device(target) != _device(self.dir) or _device(target) != _device(self.staging):
            raise UpdateError("%s is on a different disk than the backup folder %s. Set SKILL_UPDATE_HOME to a folder "
                              "on the same disk as your skills." % (short(target), short(self.dir.parent)))
        backup = self.dir / "dirs" / self._next(label)
        entry = {"kind": "dir", "target": str(target), "backup": str(backup), "label": label, "state": "pending"}
        self.entries.append(entry)
        self.save()
        try:
            _move(target, backup)
        except BaseException:  # nothing was moved: forget the entry
            self.entries.remove(entry)
            self.save()
            raise
        entry["state"] = "away"  # the old folder is complete in the backup area; the target is empty for the moment
        self.save()
        try:
            _move(new, target)
        except BaseException:
            _move(backup, target)  # put the original back; if even that fails the entry stays, so rollback can finish it
            self.entries.remove(entry)
            self.save()
            raise
        entry["state"] = "done"
        self.save()
        return backup

    def revert_swap(self, target: Path, backup: Path) -> None:
        """Undo a swap whose result failed verification. The rejected folder is set aside, not deleted."""
        aside = self.dir / "rolled-back" / self._next("rejected")
        _move(target, aside)
        _move(backup, target)
        for e in self.entries:
            if e.get("kind") == "dir" and e.get("target") == str(target) and e.get("state") == "done":
                e["state"] = "reverted"
        self.save()

    def snapshot_file(self, path: Path) -> None:
        """A full copy of a file we are about to edit, kept for manual recovery."""
        dst = self.dir / "files" / self._next(path.name)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dst)

    def record_lock(self, path: Path, before: Dict[str, Dict[str, Any]], after: Dict[str, Dict[str, Any]]) -> None:
        """Which lock entries this run changed, and how: rollback undoes exactly these and nothing else."""
        self.entries.append({"kind": "lock", "target": str(path), "before": before, "after": after, "state": "done"})
        self.save()

    def record_git(self, repo: Path, old: str, new: Optional[str], stash: Optional[str], label: str,
                   state: str = "done") -> Dict[str, Any]:
        entry = {"kind": "git", "target": str(repo), "old": old, "new": new, "stash": stash, "label": label, "state": state}
        self.entries.append(entry)
        self.save()
        return entry

    def finish(self, discard: bool = True) -> None:
        """Clean up. `discard=False` (an error or an interruption) keeps every backup, whatever --no-backup says."""
        shutil.rmtree(self.staging, ignore_errors=True)
        if discard and not self.keep_backup:  # --no-backup: replaced folders go once everything is verified
            for e in self.entries:
                if e.get("kind") == "dir" and e.get("state") == "done" and _inside(self.dir, Path(e["backup"])):
                    _discard(Path(e["backup"]))
                    e["state"] = "discarded"
            if self.entries:
                self.save()
        if not self.entries and not _has_files(self.dir / "dirs"):
            shutil.rmtree(self.dir, ignore_errors=True)
        _prune()


def _has_files(path: Path) -> bool:
    return path.is_dir() and any(path.iterdir())


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
                        "changes": len([e for e in entries if e.get("state") in ("done", "away", "pending", "stashed", "restored")]),
                        "restorable": len([e for e in entries if e.get("state") in ("done", "away", "pending", "stashed")])})
    return out


def _rollback_dir(run_dir: Path, e: Dict[str, Any], lines: List[str]) -> bool:
    backup, target, label = Path(e["backup"]), Path(e["target"]), e.get("label") or Path(e["target"]).name
    if not _inside(run_dir, backup):
        lines.append("! %s: its backup is recorded outside the run folder; skipped" % label)
        return False
    if not backup.is_dir():
        if e.get("state") != "pending":  # pending without a backup: the run stopped before anything was moved
            lines.append("! %s: the backup folder is gone" % label)
        return False
    if os.path.lexists(target):
        aside = run_dir / "rolled-back" / ("%s-%d" % (_slug(label), int(time.time())))
        _move(target, aside)
        _move(backup, target)
        carry_local_state(aside, target)  # node_modules etc. go back with the old folder
    else:
        _move(backup, target)
    return True


def _rollback_git(e: Dict[str, Any], lines: List[str]) -> bool:
    repo, label = Path(e["target"]), e.get("label") or Path(e["target"]).name
    head = git("rev-parse", "HEAD", cwd=repo).out.strip()
    if e.get("state") == "stashed" and head == e.get("old"):  # stopped after saving the edits, before the merge
        if e.get("stash") and not git("status", "--porcelain", cwd=repo).out.strip():
            git("stash", "apply", "-q", e["stash"], cwd=repo)
            return True
        lines.append("! %s: your saved edits are still in `git stash`" % label)
        return False
    if head != e.get("new"):
        lines.append("! %s: the repository has moved on since the update; left as it is" % label)
        return False
    if git("status", "--porcelain", cwd=repo).out.strip():  # edits made since the update: keep them too
        saved = git("stash", "push", "--include-untracked", "-m", "skill-update rollback", cwd=repo)
        if not saved.ok:
            lines.append("! %s: could not set your newer edits aside (%s); left as it is" % (label, first_error(saved)))
            return False
        lines.append("  %s: edits made after the update are saved in `git stash` (\"skill-update rollback\")" % label)
    r = git("reset", "--hard", "-q", e["old"], cwd=repo)
    if not r.ok:
        raise UpdateError(first_error(r))
    if e.get("stash"):
        if not git("stash", "apply", "-q", e["stash"], cwd=repo).ok:
            lines.append("! %s: restored the old version, but your saved edits could not be re-applied "
                         "(they are in `git stash`)" % label)
    return True


def _rollback_lock(e: Dict[str, Any], lines: List[str]) -> bool:
    path = Path(os.path.realpath(e["target"]))
    data = read_json(path)
    skills = data.get("skills") if isinstance(data, dict) else None
    if not isinstance(skills, dict):
        lines.append("! lock file %s could not be read; left as it is" % short(path))
        return False
    for key, after in (e.get("after") or {}).items():
        current = skills.get(key)
        if not isinstance(current, dict):
            continue
        if any(current.get(field) != value for field, value in after.items()):
            lines.append("! lock entry %s changed since the update; left as it is" % key)
            continue
        for field, old in (e.get("before") or {}).get(key, {}).items():
            if old is None:
                current.pop(field, None)
            else:
                current[field] = old
    atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return True


def rollback(run_id: Optional[str]) -> List[str]:
    """Restore everything a run changed. Returns human-readable lines describing what happened."""
    runs = list_runs()
    if not runs:
        raise UpdateError("there are no backups to restore")
    wanted = run_id or runs[-1]["id"]
    run_dir = state_dir() / "backups" / wanted
    if not _inside(state_dir() / "backups", run_dir) or not (run_dir / "manifest.json").is_file():
        raise UpdateError("no backup named %s (see: rollback --list)" % wanted)
    data = read_json(run_dir / "manifest.json")
    if not isinstance(data, dict):
        raise UpdateError("the backup %s is damaged" % wanted)
    lines: List[str] = []
    for e in reversed(data.get("entries", [])):
        if e.get("state") not in ("done", "away", "pending", "stashed"):
            continue
        label = e.get("label") or Path(str(e.get("target", ""))).name
        try:
            handler = {"dir": lambda: _rollback_dir(run_dir, e, lines), "git": lambda: _rollback_git(e, lines),
                       "lock": lambda: _rollback_lock(e, lines)}.get(e.get("kind"))
            restored = handler() if handler else False
        except (OSError, shutil.Error, UpdateError) as ex:
            lines.append("! %s: %s" % (label, ex))
            continue
        if restored:
            e["state"] = "restored"
            lines.append("restored %s" % (label if e.get("kind") != "lock" else "the npx skills lock records"))
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
