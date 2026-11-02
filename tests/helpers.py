"""Test fixtures: a throw-away HOME and a local 'GitHub' made of bare git repositories."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "skill-update" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from skillupdate.util import rmtree  # noqa: E402  (needs the path above)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
}
# Anything in the developer's own environment that could change where skill-update looks.
CLEARED = ["CLAUDE_CONFIG_DIR", "CODEX_HOME", "HERMES_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME",
           "SKILL_UPDATE_LOCK", "SKILL_UPDATE_CLAUDE", "SKILL_UPDATE_LENIENT", "NO_COLOR"]


def git(*args: str, cwd: Optional[Path] = None) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, env=dict(os.environ, **GIT_ENV), capture_output=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), r.stderr))
    return r.stdout.strip()


class Upstream:
    """A local stand-in for a GitHub repository: a work tree to commit in, and a bare repo to clone from."""

    def __init__(self, base: Path, name: str = "up") -> None:
        self.work, self.bare = base / (name + "-work"), base / (name + ".git")
        self.work.mkdir(parents=True)
        git("init", "-q", "-b", "main", cwd=self.work)
        git("init", "-q", "--bare", "-b", "main", str(self.bare))
        git("config", "uploadpack.allowFilter", "true", cwd=self.bare)
        git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=self.bare)
        git("remote", "add", "origin", str(self.bare), cwd=self.work)
        self.url = self.bare.as_uri()   # file:///C:/... on Windows: a file URL names no host

    def commit(self, files: Dict[str, Any], message: str = "change") -> str:
        """files: path -> text | bytes | (text, mode) | None (delete)."""
        executable = []
        for rel, content in files.items():
            p = self.work / rel
            if content is None:
                shutil.rmtree(p) if p.is_dir() else p.unlink()
                continue
            mode = None
            if isinstance(content, tuple):
                content, mode = content
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content if isinstance(content, bytes) else content.encode())
            if mode:
                p.chmod(mode)
                if mode & 0o111:
                    executable.append(rel)
        git("add", "-A", cwd=self.work)
        if executable:  # a file system without the executable bit (NTFS) cannot say it, so tell git directly
            git("update-index", "--chmod=+x", "--", *executable, cwd=self.work)
        git("commit", "-q", "--allow-empty", "-m", message, cwd=self.work)
        git("push", "-q", "origin", "main", cwd=self.work)
        return git("rev-parse", "HEAD", cwd=self.work)

    def tree(self, folder: str = "") -> str:
        return git("rev-parse", "HEAD:" + folder if folder else "HEAD^{tree}", cwd=self.work)


class Sandbox(unittest.TestCase):
    """Every test runs against its own HOME, state folder and git config, never the developer's."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="su-test-")).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        # Windows takes the home folder from USERPROFILE, not HOME: without it the tool would read the real one
        patch = dict(GIT_ENV, HOME=str(self.home), USERPROFILE=str(self.home), SKILL_UPDATE_HOME=str(self.tmp / "state"))
        self._env = mock.patch.dict(os.environ, patch)
        self._env.start()
        for name in CLEARED:
            os.environ.pop(name, None)
        self.addCleanup(self._env.stop)
        self.addCleanup(rmtree, self.tmp)   # also removes the read-only files git leaves behind on Windows
        self.agents = self.home / ".agents" / "skills"
        self.claude = self.home / ".claude" / "skills"

    # -- building a machine -------------------------------------------------------------------

    def upstream(self, name: str = "up") -> Upstream:
        return Upstream(self.tmp, name)

    def install(self, up: Upstream, folder: str, dest: Path) -> Path:
        """Copy a folder of the upstream work tree to `dest`, the way `npx skills add` would."""
        shutil.copytree(up.work / folder, dest, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        return dest

    def lock_entry(self, up: Upstream, folder: str, source: str = "owner/repo", tree: Optional[str] = None) -> Dict[str, Any]:
        return {"source": source, "sourceType": "github", "sourceUrl": up.url,
                "skillPath": (folder + "/SKILL.md") if folder else "SKILL.md",
                "skillFolderHash": tree or up.tree(folder),
                "installedAt": "2026-01-01T00:00:00.000Z", "updatedAt": "2026-01-01T00:00:00.000Z"}

    def write_lock(self, skills: Dict[str, Dict[str, Any]]) -> Path:
        path = self.home / ".agents" / ".skill-lock.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 3, "skills": skills, "dismissed": {}, "lastSelectedAgents": []}, indent=2) + "\n")
        return path

    def read_lock(self) -> Dict[str, Any]:
        return json.loads((self.home / ".agents" / ".skill-lock.json").read_text())

    def clone(self, up: Upstream, name: str = "pack") -> Path:
        """A git clone sitting in ~/.claude/skills, the way a hand-cloned skill pack would."""
        self.claude.mkdir(parents=True, exist_ok=True)
        dest = self.claude / name
        git("clone", "-q", up.url, str(dest))
        return dest

    def marker_script(self, name: str) -> Tuple[Path, Path]:
        """A script that leaves a marker file when run, to prove something was (not) executed."""
        marker = self.tmp / (name + ".ran")
        script = self.tmp / (name + ".sh")
        # bytes, so that Windows does not turn the first line into "#!/bin/sh\r"; git runs these through sh, also there
        script.write_bytes(("#!/bin/sh\ntouch '%s'\n" % marker.as_posix()).encode())
        script.chmod(0o755)
        return script, marker

    def program(self, name: str, source: str) -> Path:
        """An executable that runs this Python source: a script with a shebang, or a .cmd file on Windows."""
        if os.name != "nt":
            exe = self.tmp / name
            exe.write_text("#!%s\n%s" % (sys.executable, source), encoding="utf-8")
            exe.chmod(0o755)
            return exe
        script = self.tmp / (name + ".py")
        script.write_text(source, encoding="utf-8")
        exe = self.tmp / (name + ".cmd")
        exe.write_text('@"%s" "%s" %%*\r\n' % (sys.executable, script), encoding="utf-8")
        return exe

    def symlink(self, target: Any, link: Any) -> None:
        """os.symlink, or skip the test where this account may not create symlinks (Windows without developer mode)."""
        try:
            os.symlink(target, link, target_is_directory=os.path.isdir(target))
        except (OSError, NotImplementedError) as ex:
            self.skipTest("cannot create symlinks here: %s" % ex)

    # -- running the tool ---------------------------------------------------------------------

    def cli(self, *args: str) -> Tuple[int, str, str]:
        r = subprocess.run([sys.executable, str(SCRIPTS / "skill_update.py"), *args],
                           capture_output=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout, r.stderr

    def check(self, *args: str) -> Dict[str, Any]:
        rc, out, err = self.cli("check", "--json", *args)
        self.assertEqual(rc, 0, err)
        return json.loads(out)

    def item(self, report: Dict[str, Any], name: str) -> Dict[str, Any]:
        found = [i for i in report["items"] if i["name"] == name]
        self.assertTrue(found, "no item named %r in %s" % (name, [i["name"] for i in report["items"]]))
        return found[0]
