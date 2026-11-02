"""Helpers shared by every module. Standard library only."""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

# Git must never stop to ask for a password: a private repo should fail fast, not hang.
_CHILD_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "LC_ALL": "C"}
# If we are started from inside a git hook or script these point at that repository, and git would then
# stash or reset the wrong one instead of the skill folder it is run in.
_REPO_SELECTORS = frozenset({"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                             "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_PREFIX"})


class Proc:
    """A finished child process. `raw` keeps the bytes for callers that parse NUL-separated output."""

    __slots__ = ("rc", "raw", "err")

    def __init__(self, rc: int, raw: bytes, err: bytes) -> None:
        self.rc, self.raw = rc, raw
        self.err = err.decode("utf-8", "replace")

    @property
    def ok(self) -> bool:
        return self.rc == 0

    @property
    def out(self) -> str:
        return os.fsdecode(self.raw)


def run(args: Sequence[str], cwd: Optional[Path] = None, timeout: int = 120, env: Optional[dict] = None) -> Proc:
    """Run a command without a shell. Never raises: failures come back as a non-zero `rc`."""
    child_env = {k: v for k, v in os.environ.items() if k not in _REPO_SELECTORS}
    child_env.update(_CHILD_ENV)
    if env:
        child_env.update(env)
    try:
        cp = subprocess.run(
            list(args), cwd=str(cwd) if cwd else None, env=child_env, timeout=timeout,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return Proc(cp.returncode, cp.stdout, cp.stderr)
    except subprocess.TimeoutExpired:
        return Proc(124, b"", b"timed out after %d s" % timeout)
    except FileNotFoundError:
        return Proc(127, b"", ("command not found: %s" % args[0]).encode())
    except OSError as ex:
        return Proc(126, b"", str(ex).encode())


def which(name: str) -> Optional[str]:
    """Find a program on PATH. The current folder is never searched: Windows looks there first for a bare name,
    and a skill folder we run git in could hold a git.exe of its own."""
    exts = [""]
    if os.name == "nt":
        exts = [e for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep) if e] + [""]
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        folder = folder.strip('"')
        if not folder or not os.path.isabs(folder):  # "" and "." mean the current folder; relative entries are not trusted
            continue
        for ext in exts:
            candidate = os.path.join(folder, name + ext)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def home() -> Path:
    return Path(os.path.expanduser("~"))


def state_dir() -> Path:
    """Where the repo cache and the backups live. Override with SKILL_UPDATE_HOME."""
    env = os.environ.get("SKILL_UPDATE_HOME")
    return Path(env).expanduser() if env else home() / ".skill-update"


def _short(p: str, h: str, ps: Any) -> str:
    """`short` with the path flavour (posixpath or ntpath) passed in, so Windows paths can be tested anywhere."""
    windows = ps.sep == "\\"
    key, home_key = ps.normcase(p), ps.normcase(h).rstrip(ps.sep)  # Windows paths ignore case and take either slash
    if home_key and key == home_key:
        return "~"
    if home_key and key.startswith(home_key + ps.sep):
        rest = p[len(home_key):]
        return "~" + (rest.replace("\\", "/") if windows else rest)
    return p.replace("\\", "/") if windows else p


def short(path: Any) -> str:
    """Show ~ instead of the home directory, so output is portable and does not leak the user name.
    Windows paths are shown with forward slashes, like everywhere else."""
    return _short(str(path), str(home()), os.path)


def sanitize_name(name: str) -> str:
    """The folder name the `skills` CLI gives a skill: lowercase, only [a-z0-9._-]."""
    s = re.sub(r"[^a-z0-9._]+", "-", name.lower()).strip(".-")
    return s[:255] or "unnamed-skill"


_CREDENTIALS = re.compile(r"(://)[^/@\s]+@")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # C0/C1 controls except tab and newline


def clean(text: str) -> str:
    """Drop terminal control characters. Names and messages that came from upstream repositories or
    skill folders are untrusted: an escape sequence in one must not be able to rewrite what we print."""
    return _CONTROL.sub("", text)


def scrub(text: str, limit: int = 300) -> str:
    """One-line, credential-free, control-character-free version of a tool message that is safe to print."""
    text = " ".join(_CREDENTIALS.sub(r"\1***@", clean(text or "")).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def first_error(p: Proc) -> str:
    lines = [ln.strip() for ln in (p.err or p.out).splitlines() if ln.strip()]
    for ln in lines:
        if ln.startswith(("fatal:", "error:")):
            return scrub(ln)
    return scrub(lines[0]) if lines else "exit code %d" % p.rc


def now_iso() -> str:
    """Timestamp in the format the skills lock file uses (UTC, milliseconds, trailing Z)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def read_json(path: Path) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def rmtree(path: Any) -> None:
    """Remove a folder with everything in it, read-only files included (git marks its objects read-only on
    Windows, which makes shutil.rmtree stop). Whatever still cannot be removed is left behind quietly."""
    root = os.path.abspath(str(path))
    if not os.path.lexists(root):
        return

    def force(func: Any, p: str, _exc: Any) -> None:
        try:
            for fix in (p, os.path.dirname(p)):
                if fix == root or fix.startswith(root + os.sep):  # never change anything outside the tree being removed
                    os.chmod(fix, stat.S_IRWXU)
            func(p)
        except OSError:
            pass

    if sys.version_info >= (3, 12):
        shutil.rmtree(root, onexc=force)
    else:
        shutil.rmtree(root, onerror=force)


def _replace_file(tmp: str, path: Path) -> None:
    """os.replace; on Windows it is refused for a moment while another program has the file open, so retry."""
    for wait in (0.05, 0.1, 0.2, 0.4):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if os.name != "nt":
                raise
            time.sleep(wait)
    os.replace(tmp, path)


def atomic_write(path: Path, text: str) -> None:
    """Write via a temp file in the same folder so a crash never leaves a half-written file.
    A symlinked file (dotfile managers do that) is written through the link, so the link survives."""
    path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        try:
            os.chmod(tmp, path.stat().st_mode & 0o777)
        except OSError:
            pass
        _replace_file(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def enable_ansi() -> bool:
    """Make a Windows console understand ANSI escape codes (Windows 10 and later). False where it cannot,
    so that the escape codes are not printed as text."""
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.windll.kernel32
        kernel.GetStdHandle.restype = wintypes.HANDLE
        handle = kernel.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = wintypes.DWORD()
        if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(mode.value & 0x0004) or bool(kernel.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def frontmatter_name(skill_md: Path) -> Optional[str]:
    """The `name:` value from a SKILL.md frontmatter block, or None."""
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    for line in text.splitlines()[1:80]:
        if line.strip() == "---":
            break
        m = re.match(r"name\s*:\s*(.+?)\s*$", line)
        if m:
            return m.group(1).strip("'\"")
    return None
