"""Hash a folder exactly like git hashes a tree.

`npx skills` records, for every skill it installs, the git tree hash of the skill's folder
(`skillFolderHash`). Reproducing that hash locally lets us tell, without any network call,
whether an installed folder is identical to a given upstream folder, and which files differ.
"""
from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, Tuple

from .model import Changes

# relative path (posix) -> (git mode, blob sha1)
Manifest = Dict[str, Tuple[str, str]]

# Never part of a skill's source: dropped from hashes.
_JUNK_NAMES = frozenset({".git", ".DS_Store", "__pycache__", "Thumbs.db"})
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


def manifest(root: str) -> Manifest:
    """List every file under `root` the way git would store it. Symlinks are hashed, never followed."""
    out: Manifest = {}

    def walk(path: str, prefix: str) -> None:
        with os.scandir(path) as it:
            entries = sorted(it, key=lambda e: e.name)
        for e in entries:
            if _skip(e.name):
                continue
            rel = prefix + e.name
            if e.is_symlink():
                out[rel] = ("120000", blob_sha(os.fsencode(os.readlink(e.path))))
            elif e.is_dir(follow_symlinks=False):
                walk(e.path, rel + "/")
            elif e.is_file(follow_symlinks=False):
                st = e.stat(follow_symlinks=False)
                out[rel] = ("100755" if st.st_mode & 0o100 else "100644", _file_blob_sha(e.path, st.st_size))

    walk(str(root), "")
    return out


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
