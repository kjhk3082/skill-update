"""Hash a folder exactly like git hashes a tree.

`npx skills` records, for every skill it installs, the git tree hash of the skill's folder
(`skillFolderHash`). Reproducing that hash locally lets us tell, without any network call,
whether an installed folder is identical to a given upstream folder, and which files differ.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set, Tuple

from .model import Changes
from .util import is_junction

# relative path (posix) -> (git mode, blob sha1)
Manifest = Dict[str, Tuple[str, str]]

# Never part of a skill's source: dropped from hashes.
_JUNK_NAMES = frozenset({".git", ".DS_Store", "__pycache__", "Thumbs.db", "desktop.ini"})
_JUNK_SUFFIXES = (".pyc",)
# Local state a user may have created inside a skill (npm install, venv). Ignored in hashes
# and carried over when a skill folder is replaced.
LOCAL_STATE_DIRS = ("node_modules", ".venv", "venv")


def _skip(name: str) -> bool:
    return name in _JUNK_NAMES or name in LOCAL_STATE_DIRS or name.endswith(_JUNK_SUFFIXES)


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _file_blob_sha(path: str, size: int) -> str:
    h = hashlib.sha1(b"blob %d\0" % size)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_lenient() -> bool:
    """Whether to judge folders the way a file system without git's file modes needs.

    Windows has no executable bit, and git for Windows usually checks files out with CRLF line endings
    (core.autocrlf=true), which `npx skills` inherits because it clones with the user's git settings.
    Compared strictly, every installed skill would look edited. On by default on Windows;
    SKILL_UPDATE_LENIENT=1 or 0 forces it either way (tests, unusual setups).
    """
    forced = os.environ.get("SKILL_UPDATE_LENIENT")
    return forced == "1" if forced in ("0", "1") else os.name == "nt"


_TEXT_PROBE = 8000        # git calls a file binary when it finds a NUL byte in its first 8000 bytes
_NORMALIZE_LIMIT = 16 << 20


def _hash_file(path: str, size: int, both: bool) -> Tuple[str, Optional[str], bool]:
    """A file's blob hash and, when `both`, the hash it would have with CRLF turned into LF (None unless the
    file is text with CRLF line endings in it) and whether it is a script (starts with `#!`)."""
    if not both or size > _NORMALIZE_LIMIT:
        return _file_blob_sha(path, size), None, False
    with open(path, "rb") as f:
        data = f.read()
    alt = None
    if b"\r\n" in data and b"\0" not in data[:_TEXT_PROBE]:
        alt = blob_sha(data.replace(b"\r\n", b"\n"))
    return blob_sha(data), alt, data[:2] == b"#!"


_FILE_MODES = ("100644", "100755")
_MAX_UNSURE = 10          # modes tried both ways: at most 2**10 combinations


@dataclass
class Scan:
    """A folder as it is on disk, plus what is needed to judge it against what git holds for it."""
    files: Manifest
    lf: Dict[str, str] = field(default_factory=dict)  # path -> hash with CRLF turned into LF (lenient scans only)
    scripts: Set[str] = field(default_factory=set)    # files that start with `#!` (lenient scans only)
    lenient: bool = False

    def view(self, reference: Manifest) -> Manifest:
        """The folder as git would hold it, judged against the upstream listing it should match.

        Strict: exactly what is on disk. Lenient: this file system cannot say whether a file is executable and
        may have changed its line endings, so a file whose content equals upstream's, once CRLF is undone,
        counts as that file with the mode git recorded. A file upstream has but with other content (changed
        upstream since, or edited here) keeps upstream's mode, because modes rarely change between versions.
        A file upstream does not have is plain. A symlink checked out as a text file matches the link it stands for.
        """
        if not self.lenient:
            return self.files
        out: Manifest = {}
        for path, (mode, sha) in self.files.items():
            if mode == "120000":
                out[path] = (mode, sha)
                continue
            want, alt = reference.get(path), self.lf.get(path)
            if want is not None and sha != want[1] and alt == want[1]:
                sha = want[1]                       # only the line endings differ
            if want is not None and sha == want[1]:
                out[path] = (want[0], sha)          # the same file: the mode git recorded
            elif want is not None and want[0] in _FILE_MODES:
                out[path] = (want[0], alt or sha)   # another version of it: git stores LF endings, and the mode is assumed
            else:
                out[path] = ("100644", alt or sha)  # new here: nothing to go by
        return out

    def matches(self, reference: Manifest, target: str) -> bool:
        """Whether the folder is the version whose tree hash is `target`, when only the newer listing is at hand.

        Strict: the folder's own hash. Lenient: the view, and then the modes the view could only assume. The
        executable bit of a file upstream no longer has, of a script, or of a file that is executable now may
        have been the other way in the old version, so those are tried both ways (a few files at a time).
        """
        base = self.view(reference)
        if tree_sha(base) == target:
            return True
        if not self.lenient:
            return False
        unsure = sorted(p for p, (mode, _sha) in base.items()
                        if mode in _FILE_MODES and (p not in reference or mode == "100755" or p in self.scripts))
        if not unsure or len(unsure) > _MAX_UNSURE:
            return False
        for mask in range(1, 1 << len(unsure)):
            trial = dict(base)
            for i, path in enumerate(unsure):
                if mask >> i & 1:
                    mode, sha = trial[path]
                    trial[path] = ("100644" if mode == "100755" else "100755", sha)
            if tree_sha(trial) == target:
                return True
        return False


def scan(root: str, lenient: Optional[bool] = None) -> Scan:
    """List every file under `root` the way git would store it. Symlinks are hashed, never followed."""
    judge = is_lenient() if lenient is None else lenient
    files: Manifest = {}
    lf: Dict[str, str] = {}
    scripts: Set[str] = set()

    def walk(path: str, prefix: str) -> None:
        with os.scandir(path) as it:
            entries = sorted(it, key=lambda e: e.name)
        for e in entries:
            if _skip(e.name):
                continue
            rel = prefix + e.name
            if e.is_symlink() or is_junction(e.path):  # a junction is a link too; walking into one could loop forever
                files[rel] = ("120000", blob_sha(os.fsencode(os.readlink(e.path))))
            elif e.is_dir(follow_symlinks=False):
                walk(e.path, rel + "/")
            elif e.is_file(follow_symlinks=False):
                st = e.stat(follow_symlinks=False)
                sha, alt, script = _hash_file(e.path, st.st_size, judge)
                files[rel] = ("100755" if st.st_mode & 0o100 else "100644", sha)
                if alt is not None:
                    lf[rel] = alt
                if script:
                    scripts.add(rel)

    walk(str(root), "")
    return Scan(files, lf, scripts, judge)


def manifest(root: str) -> Manifest:
    """The files under `root` exactly as they are on disk (a strict scan)."""
    return scan(root, lenient=False).files


def view_of(root: str, reference: Manifest) -> Manifest:
    """The folder at `root` as git would hold it, judged against the upstream listing it should match."""
    return scan(root).view(reference)


def _hash_node(node: Dict[str, Any]) -> str:
    rows = []
    for name, val in node.items():
        nb = os.fsencode(name)
        if isinstance(val, dict):  # git sorts a sub-tree as if its name ended with "/"
            rows.append((nb + b"/", b"40000", nb, bytes.fromhex(_hash_node(val))))
        else:
            mode, sha = val
            rows.append((nb, mode.encode(), nb, bytes.fromhex(sha)))
    rows.sort(key=lambda r: r[0])
    body = b"".join(mode + b" " + nb + b"\0" + sha for _, mode, nb, sha in rows)
    return hashlib.sha1(b"tree %d\0" % len(body) + body).hexdigest()


def tree_sha(m: Manifest) -> str:
    """The git tree hash of a manifest (the empty manifest gives git's empty-tree hash)."""
    root: Dict[str, Any] = {}
    for rel, entry in m.items():
        parts = rel.split("/")
        node = root
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = entry
    return _hash_node(root)


# What the `npx skills add` installer leaves out when it copies a skill (EXCLUDE_FILES / EXCLUDE_DIRS in
# its installer), and what it does to symlinks (it copies the file the link points to). A clean install
# made by the CLI therefore differs from the upstream folder in exactly these ways, without being edited.
CLI_EXCLUDED_FILES = frozenset({"metadata.json"})
CLI_EXCLUDED_DIRS = frozenset({".git", "__pycache__", "__pypackages__"})


def filtered(listing: Manifest) -> Manifest:
    """An upstream listing without the junk that is ignored on disk, so both sides are compared like for like."""
    return {p: v for p, v in listing.items() if not any(_skip(part) for part in p.split("/"))}


def cli_view(listing: Manifest) -> Tuple[Manifest, bool]:
    """What the CLI would leave on disk for an upstream listing, and whether that cannot be predicted exactly
    (a symlink becomes a copy of its target, whose hash is not in the listing)."""
    out: Manifest = {}
    unpredictable = False
    for path, (mode, sha) in listing.items():
        parts = path.split("/")
        if parts[-1] in CLI_EXCLUDED_FILES or any(p in CLI_EXCLUDED_DIRS for p in parts[:-1]):
            continue
        if mode == "120000":
            unpredictable = True
            continue
        out[path] = (mode, sha)
    return out, unpredictable


def diff(installed: Manifest, upstream: Manifest) -> Changes:
    """What replacing `installed` with `upstream` would add, overwrite and delete."""
    ch = Changes()
    for path in sorted(upstream):
        if path not in installed:
            ch.added.append(path)
        elif installed[path] != upstream[path]:
            ch.modified.append(path)
    ch.removed = sorted(p for p in installed if p not in upstream)
    return ch
