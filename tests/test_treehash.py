import tempfile
import unittest
from pathlib import Path

from helpers import Sandbox, git  # noqa: F401  (also puts the package on sys.path)

from skillupdate import treehash
from skillupdate.util import sanitize_name, scrub


class TreeHashTests(Sandbox):
    def test_matches_git_write_tree(self):
        d = self.tmp / "tree"
        (d / "a" / "b").mkdir(parents=True)
        (d / "a" / "b" / "x.txt").write_text("hi\n")
        (d / "run.sh").write_text("#!/bin/sh\n")
        (d / "run.sh").chmod(0o755)
        (d / "a.txt").write_text("A")
        (d / "a-b").write_text("dash")       # sorts between the folder `a` and the file `a.txt`
        (d / "ünï.md").write_text("unicode name")
        self.symlink("a.txt", d / "link")
        (d / "emptydir").mkdir()             # git does not store empty folders
        git("init", "-q", cwd=d)
        git("add", "-A", cwd=d)
        self.assertEqual(treehash.tree_sha(treehash.manifest(str(d))), git("write-tree", cwd=d))

    def test_junk_and_local_state_are_ignored(self):
        d = self.tmp / "skill"
        d.mkdir()
        (d / "SKILL.md").write_text("x")
        clean = treehash.tree_sha(treehash.manifest(str(d)))
        for junk in (".DS_Store", "mod.pyc"):
            (d / junk).write_text("junk")
        for folder in ("__pycache__", "node_modules", ".venv", "scripts/node_modules"):
            (d / folder).mkdir(parents=True)
            (d / folder / "f").write_text("junk")
        self.assertEqual(treehash.tree_sha(treehash.manifest(str(d))), clean)

    def test_empty_folder_is_gits_empty_tree(self):
        d = self.tmp / "empty"
        d.mkdir()
        self.assertEqual(treehash.tree_sha(treehash.manifest(str(d))), "4b825dc642cb6eb9a060e54bf8d69288fbee4904")

    def test_diff_lists_added_modified_removed(self):
        a = {"keep": ("100644", "1"), "edit": ("100644", "2"), "gone": ("100644", "3"), "exec": ("100644", "4")}
        b = {"keep": ("100644", "1"), "edit": ("100644", "9"), "new": ("100644", "5"), "exec": ("100755", "4")}
        ch = treehash.diff(a, b)
        self.assertEqual((ch.added, ch.modified, ch.removed), (["new"], ["edit", "exec"], ["gone"]))


class UtilTests(unittest.TestCase):
    def test_sanitize_name_follows_the_skills_cli(self):
        for raw, want in {"omd:add-reference": "omd-add-reference", "My Skill!": "my-skill", "a_b.c": "a_b.c",
                          "--x--": "x", "": "unnamed-skill", "UPPER": "upper"}.items():
            self.assertEqual(sanitize_name(raw), want)

    def test_scrub_hides_credentials_and_truncates(self):
        self.assertNotIn("hunter2", scrub("fatal: could not read https://user:hunter2@example.com/x.git"))
        self.assertLessEqual(len(scrub("x" * 1000)), 300)


if __name__ == "__main__":
    unittest.main()
