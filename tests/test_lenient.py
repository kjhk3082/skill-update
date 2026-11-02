"""Comparing a folder on a file system that cannot hold git's file modes and whose line endings were converted.

Windows has no executable bit, and git for Windows usually checks files out with CRLF line endings
(core.autocrlf=true), which `npx skills` inherits because it clones with the user's git settings. Without
help, every installed skill would look edited. The rules are plain logic, so they run everywhere: the
tests below switch them on explicitly instead of waiting for a Windows machine.
"""
import json
import os
import unittest

from helpers import Sandbox

from skillupdate import treehash
from skillupdate.treehash import blob_sha, diff, tree_sha


class ViewTests(Sandbox):
    def folder(self, files):
        d = self.tmp / "skill"
        d.mkdir()
        for rel, data in files.items():
            p = d / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            p.chmod(0o644)
        return str(d)

    @staticmethod
    def listing(files):
        """What git holds for these files: path -> (mode, blob hash)."""
        return {p: (mode, blob_sha(data)) for p, (mode, data) in files.items()}

    def test_strict_mode_is_exactly_what_is_on_disk(self):
        root = self.folder({"a.md": b"a\r\n"})
        ref = self.listing({"a.md": ("100644", b"a\n")})
        self.assertEqual(treehash.scan(root, lenient=False).view(ref), treehash.manifest(root))

    def test_the_executable_bit_is_taken_from_upstream_when_the_content_is_the_same(self):
        root = self.folder({"run.sh": b"echo\n"})
        ref = self.listing({"run.sh": ("100755", b"echo\n")})
        self.assertEqual(treehash.scan(root, lenient=True).view(ref), ref)
        self.assertNotEqual(treehash.scan(root, lenient=False).view(ref), ref)

    def test_crlf_line_endings_added_by_git_for_windows_are_undone(self):
        root = self.folder({"SKILL.md": b"one\r\ntwo\r\n"})
        ref = self.listing({"SKILL.md": ("100644", b"one\ntwo\n")})
        self.assertEqual(tree_sha(treehash.scan(root, lenient=True).view(ref)), tree_sha(ref))
        self.assertNotEqual(tree_sha(treehash.scan(root, lenient=False).view(ref)), tree_sha(ref))

    def test_a_file_that_has_crlf_upstream_is_left_as_it_is(self):
        root = self.folder({"setup.bat": b"echo hi\r\n"})
        ref = self.listing({"setup.bat": ("100644", b"echo hi\r\n")})
        self.assertEqual(treehash.scan(root, lenient=True).view(ref), ref)

    def test_a_real_edit_is_still_an_edit(self):
        root = self.folder({"SKILL.md": b"changed\r\n", "keep.md": b"keep\r\n"})
        ref = self.listing({"SKILL.md": ("100644", b"one\n"), "keep.md": ("100644", b"keep\n")})
        changes = diff(treehash.scan(root, lenient=True).view(ref), ref)
        self.assertEqual((changes.modified, changes.added, changes.removed), (["SKILL.md"], [], []))

    def test_binary_files_are_never_rewritten(self):
        root = self.folder({"logo.bin": b"\x00\r\n\x01"})
        ref = self.listing({"logo.bin": ("100644", b"\x00\n\x01")})
        self.assertNotEqual(treehash.scan(root, lenient=True).view(ref), ref)

    def test_a_symlink_that_became_a_text_file_still_matches_the_link(self):
        root = self.folder({"AGENTS.md": b"CLAUDE.md"})
        ref = {"AGENTS.md": ("120000", blob_sha(b"CLAUDE.md"))}
        self.assertEqual(treehash.scan(root, lenient=True).view(ref), ref)

    def test_a_file_upstream_does_not_have_is_a_plain_new_file(self):
        root = self.folder({"mine.md": b"mine\r\n"})
        view = treehash.scan(root, lenient=True).view({})
        self.assertEqual(view, {"mine.md": ("100644", blob_sha(b"mine\n"))})
        self.assertEqual(diff(view, {}).removed, ["mine.md"])

    def test_the_switch_follows_the_platform_unless_overridden(self):
        saved = os.environ.pop("SKILL_UPDATE_LENIENT", None)
        self.addCleanup(lambda: os.environ.__setitem__("SKILL_UPDATE_LENIENT", saved) if saved is not None else None)
        self.assertEqual(treehash.is_lenient(), os.name == "nt")
        os.environ["SKILL_UPDATE_LENIENT"] = "1"
        self.assertTrue(treehash.is_lenient())
        os.environ["SKILL_UPDATE_LENIENT"] = "0"
        self.assertFalse(treehash.is_lenient())


class LenientLockTests(Sandbox):
    """An `npx skills` install as a Windows machine leaves it, checked and updated end to end."""

    def setUp(self):
        super().setUp()
        os.environ["SKILL_UPDATE_LENIENT"] = "1"  # the sandbox restores the environment afterwards

    def setup_skill(self):
        up = self.upstream()
        up.commit({"skills/demo/SKILL.md": "---\nname: demo\n---\nv1\n", "skills/demo/run.sh": ("#!/bin/sh\necho hi\n", 0o755),
                   "skills/demo/notes.md": "one\ntwo\n"})
        installed = self.install(up, "skills/demo", self.agents / "demo")
        self.write_lock({"demo": self.lock_entry(up, "skills/demo")})
        for p in installed.rglob("*"):  # what git for Windows and NTFS make of it
            if p.is_file():
                p.write_bytes(p.read_bytes().replace(b"\n", b"\r\n"))
                p.chmod(0o644)
        return up, installed

    def test_an_untouched_windows_install_is_current_and_not_edited(self):
        self.setup_skill()
        it = self.item(self.check(), "demo")
        self.assertEqual((it["status"], it["modified"]), ("current", False))
        self.assertEqual([n for n in it["notes"] if "local edits" in n], [])

    def test_without_the_switch_the_same_install_would_look_edited(self):
        self.setup_skill()
        os.environ["SKILL_UPDATE_LENIENT"] = "0"
        it = self.item(self.check(), "demo")     # upstream is unchanged, so this is reported as a local edit
        self.assertTrue(any("local edits" in n for n in it["notes"]))

    def test_an_update_replaces_the_folder_and_verifies_it(self):
        up, installed = self.setup_skill()
        up.commit({"skills/demo/SKILL.md": "---\nname: demo\n---\nv2\n"})
        it = self.item(self.check(), "demo")
        self.assertEqual((it["status"], it["modified"]), ("outdated", False))
        self.assertEqual(it["changes"]["modified"], 1)       # only SKILL.md: the other files differ in form, not content
        rc, out, err = self.cli("update", "--yes", "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["results"][0]["outcome"], "updated")
        self.assertIn("v2", (installed / "SKILL.md").read_text())
        self.assertNotIn(b"\r\n", (installed / "notes.md").read_bytes())   # the new files are upstream's own bytes
        self.assertEqual(self.read_lock()["skills"]["demo"]["skillFolderHash"], up.tree("skills/demo"))
        self.assertEqual(self.item(self.check(), "demo")["status"], "current")

    def test_a_real_edit_is_still_reported_and_backed_up(self):
        up, installed = self.setup_skill()
        (installed / "notes.md").write_bytes(b"my own words\r\n")
        up.commit({"skills/demo/SKILL.md": "---\nname: demo\n---\nv2\n"})
        it = self.item(self.check(), "demo")
        self.assertEqual((it["status"], it["modified"]), ("outdated", True))


if __name__ == "__main__":
    unittest.main()
