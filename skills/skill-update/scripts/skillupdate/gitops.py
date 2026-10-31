"""Everything that talks to a remote goes through `git`.

Python's own HTTPS stack is deliberately not used: on many machines (for example the python.org
build on macOS) it has no CA bundle and fails with CERTIFICATE_VERIFY_FAILED, while `git` works.
"""
from __future__ import annotations

import hashlib
import os
import posixpath
import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .treehash import Manifest
from .util import Proc, first_error, frontmatter_name, run, state_dir


class GitError(Exception):
    pass


def git(*args: str, cwd: Optional[Path] = None, timeout: int = 120) -> Proc:
    return run(["git", *args], cwd=cwd, timeout=timeout)


# Values from lock files and manifests end up on a git command line, so they are checked first:
# a URL such as `--upload-pack=...` or `ext::sh -c ...` would otherwise make git run a command.
_URL_OK = re.compile(r"^(?:https?|ssh|git|file)://\S+$|^[\w.-]+@[\w.-]+:\S+$")
_REF_OK = re.compile(r"^[\w][\w./-]*$")


def safe_url(url: str) -> bool:
    return bool(_URL_OK.match(url))


def safe_ref(ref: Optional[str]) -> bool:
    return ref is None or bool(_REF_OK.match(ref))


def safe_folder(folder: str) -> bool:
    """A folder inside a repository: relative, normalized, never leaving the repository."""
    if folder == "":
        return True
    return (posixpath.normpath(folder) == folder and not folder.startswith(("/", "-", "../"))
            and folder != ".." and "\0" not in folder)


def git_version() -> Optional[Tuple[int, ...]]:
    r = run(["git", "--version"], timeout=20)
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", r.out) if r.ok else None
    return tuple(int(x) for x in m.groups() if x is not None) if m else None


class RemoteCache:
    """A partial clone of a skills repository kept in ~/.skill-update/cache.

    The clone has no file contents, only the commit and its trees, so asking "what is the hash of
    folder X at the latest commit?" costs one small fetch. File contents are downloaded later, in a
    single batch, and only for the folders that are actually going to be installed.
    """

    def __init__(self, url: str, ref: Optional[str] = None) -> None:
        self.url, self.ref = url, ref
        tag = hashlib.sha1(("%s\0%s" % (url, ref or "")).encode()).hexdigest()[:10]
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", re.sub(r"^[a-z+]+://", "", url)).strip("_")[-60:]
        self.dir = state_dir() / "cache" / "repos" / ("%s-%s" % (slug, tag))
        self.commit = ""
        self._names: Optional[Dict[str, Optional[str]]] = None

    # -- refreshing ---------------------------------------------------------------------------

    def sync(self) -> str:
        """Fetch the newest commit of the source and return its hash. Raises GitError."""
        if not safe_url(self.url) or not safe_ref(self.ref):
            raise GitError("unsupported source address in the lock file; refusing to pass it to git")
        fresh = False
        for attempt in (1, 2):
            if not (self.dir / ".git").exists():
                self.dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.rmtree(self.dir, ignore_errors=True)
                r = git("clone", "--quiet", "--filter=blob:none", "--depth=1", "--no-checkout", "--sparse",
                        "--", self.url, str(self.dir), timeout=300)
                if not r.ok:
                    raise GitError(first_error(r))
                fresh = True
            if fresh and not self.ref:
                r = git("update-ref", "refs/su/head", "HEAD", cwd=self.dir)
            else:
                r = git("fetch", "--quiet", "--depth=1", "--filter=blob:none", "origin",
                        "+%s:refs/su/head" % (self.ref or "HEAD"), cwd=self.dir, timeout=300)
            if r.ok:
                break
            if attempt == 2:
                raise GitError(first_error(r))
            shutil.rmtree(self.dir, ignore_errors=True)  # a damaged cache is cheap to rebuild once
        rp = git("rev-parse", "--verify", "-q", "refs/su/head^{commit}", cwd=self.dir)
        if not rp.ok:
            raise GitError("the source has no commits")
        self.commit = rp.out.strip()
        return self.commit

    # -- reading the tree (no file contents needed) -------------------------------------------

    def tree(self, folder: str) -> Optional[str]:
        """Tree hash of `folder` ('' = repository root) at the latest commit, or None if it is gone."""
        spec = "%s:%s" % (self.commit, folder) if folder else "%s^{tree}" % self.commit
        r = git("rev-parse", "--verify", "-q", spec, cwd=self.dir)
        if not r.ok:
            return None
        sha = r.out.strip()
        kind = git("cat-file", "-t", sha, cwd=self.dir)
        return sha if kind.ok and kind.out.strip() == "tree" else None

    def listing(self, folder: str) -> Tuple[Manifest, bool]:
        """Every file under `folder` as (mode, blob hash), plus whether git submodules were skipped."""
        treeish = "%s:%s" % (self.commit, folder) if folder else self.commit
        r = git("ls-tree", "-r", "-z", treeish, cwd=self.dir)
        if not r.ok:
            raise GitError(first_error(r))
        out: Manifest = {}
        submodules = False
        for rec in r.raw.split(b"\0"):
            if not rec:
                continue
            meta, _, path = rec.partition(b"\t")
            mode, typ, sha = meta.decode().split(" ")
            if typ == "commit":
                submodules = True
                continue
            out[os.fsdecode(path)] = (mode, sha)
        return out, submodules

    def skill_folders(self) -> List[str]:
        """Every folder in the repository that contains a SKILL.md."""
        r = git("ls-tree", "-r", "--name-only", "-z", self.commit, cwd=self.dir)
        names = [os.fsdecode(n) for n in r.raw.split(b"\0") if n]
        return sorted({posixpath.dirname(n) for n in names if posixpath.basename(n) == "SKILL.md"})

    def skill_names(self) -> Dict[str, Optional[str]]:
        """folder -> `name:` from its SKILL.md, for every skill in the repository (one batched download)."""
        if self._names is None:
            r = git("sparse-checkout", "set", "--no-cone", "SKILL.md", cwd=self.dir)
            if r.ok:
                r = git("reset", "--hard", "-q", "refs/su/head", cwd=self.dir, timeout=300)
            names: Dict[str, Optional[str]] = {}
            if r.ok:
                for folder in self.skill_folders():
                    names[folder] = frontmatter_name(self.dir / folder / "SKILL.md")
            self._names = names
        return self._names

    # -- getting the files --------------------------------------------------------------------

    def materialize(self, folders: List[str]) -> Path:
        """Check out exactly these folders (one batched download) and return the clone's root."""
        if not all(safe_folder(f) for f in folders):
            raise GitError("unsafe folder name; refusing to check it out")
        if "" in folders:
            r = git("sparse-checkout", "disable", cwd=self.dir)
        else:
            r = git("sparse-checkout", "set", "--cone", "--", *sorted(set(folders)), cwd=self.dir)
        if not r.ok:
            raise GitError(first_error(r))
        r = git("reset", "--hard", "-q", "refs/su/head", cwd=self.dir, timeout=600)
        if not r.ok:
            raise GitError(first_error(r))
        return self.dir

    def folder_path(self, folder: str) -> Path:
        return self.dir / folder if folder else self.dir
