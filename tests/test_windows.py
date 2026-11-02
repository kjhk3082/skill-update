"""The parts of running on Windows that are plain logic, so they are tested everywhere.

What only a real Windows can show (file locks, the console, the installer) is covered by the CI jobs that
run on Windows.
"""
import json
import ntpath
import os
import posixpath
import unittest
from unittest import mock

from helpers import Sandbox

from skillupdate import backup, util


class ShortTests(unittest.TestCase):
    def test_windows_paths_get_a_tilde_and_forward_slashes(self):
        h = "C:\\Users\\Me"
        self.assertEqual(util._short("C:\\Users\\Me\\.claude\\skills\\x", h, ntpath), "~/.claude/skills/x")
        self.assertEqual(util._short("c:\\users\\me\\.agents", h, ntpath), "~/.agents")  # drive and folders ignore case
        self.assertEqual(util._short("C:\\Users\\Me", h, ntpath), "~")
        self.assertEqual(util._short("C:\\Users\\Me2\\x", h, ntpath), "C:/Users/Me2/x")  # another user's folder
        self.assertEqual(util._short("D:\\skills\\x", h, ntpath), "D:/skills/x")

    def test_posix_paths_keep_their_characters(self):
        self.assertEqual(util._short("/home/me/.claude/x", "/home/me", posixpath), "~/.claude/x")
        self.assertEqual(util._short("/home/me/a\\b", "/home/me", posixpath), "~/a\\b")  # an ordinary character there
        self.assertEqual(util._short("/home/meme/x", "/home/me", posixpath), "/home/meme/x")


class MoveTests(Sandbox):
    def folders(self):
        src, dst = self.tmp / "a", self.tmp / "b"
        src.mkdir()
        return src, dst

    def test_a_folder_that_windows_holds_open_is_retried(self):
        src, dst = self.folders()
        real, calls = os.rename, []

        def flaky(a, b):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError(13, "The process cannot access the file because it is being used by another process")
            return real(a, b)

        with mock.patch.object(backup, "_WINDOWS", True), mock.patch.object(backup.time, "sleep") as sleep, \
                mock.patch.object(os, "rename", flaky):
            backup._move(src, dst)
        self.assertTrue(dst.is_dir())
        self.assertEqual((len(calls), sleep.call_count), (3, 2))

    def test_it_gives_up_with_advice_and_leaves_the_folder_where_it_was(self):
        src, dst = self.folders()
        with mock.patch.object(backup, "_WINDOWS", True), mock.patch.object(backup.time, "sleep"), \
                mock.patch.object(os, "rename", side_effect=PermissionError(13, "in use")):
            with self.assertRaises(backup.UpdateError) as cm:
                backup._move(src, dst)
        self.assertIn("open", str(cm.exception))
        self.assertTrue(src.is_dir())

    def test_elsewhere_a_permission_error_is_not_retried(self):
        src, dst = self.folders()
        with mock.patch.object(backup, "_WINDOWS", False), mock.patch.object(os, "rename",
                                                                             side_effect=PermissionError(13, "no")) as rename:
            with self.assertRaises(PermissionError):
                backup._move(src, dst)
        self.assertEqual(rename.call_count, 1)


class RmtreeTests(Sandbox):
    def test_read_only_files_and_folders_are_removed(self):
        root = self.tmp / "tree"
        (root / "ro").mkdir(parents=True)
        (root / "ro" / "f").write_text("x")
        (root / "ro" / "f").chmod(0o444)   # git marks its objects read-only on Windows
        (root / "ro").chmod(0o555)         # a read-only folder: nothing in it can go until it is writable
        util.rmtree(root)
        self.assertFalse(root.exists())

    def test_a_missing_folder_is_not_an_error(self):
        util.rmtree(self.tmp / "never-existed")


class WhichTests(Sandbox):
    def test_the_current_folder_is_never_searched(self):
        prog = self.program("evilprog", "pass\n")
        here = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, here)
        with mock.patch.dict(os.environ, {"PATH": os.pathsep.join(["", ".", str(self.tmp / "nowhere")])}):
            self.assertIsNone(util.which("evilprog"))
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp)}):
            self.assertEqual(os.path.normcase(util.which("evilprog")), os.path.normcase(str(prog)))

    def test_a_missing_program_is_none(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp)}):
            self.assertIsNone(util.which("no-such-program"))


class CacheConfigTests(Sandbox):
    def test_the_users_line_ending_settings_do_not_reach_the_cache(self):
        cfg = self.tmp / "gitconfig"
        cfg.write_text("[core]\n\tautocrlf = true\n")
        os.environ["GIT_CONFIG_GLOBAL"] = str(cfg)   # the tool's git sees a Windows-style setup; the sandbox restores it
        up = self.upstream()
        up.commit({"skills/demo/SKILL.md": "---\nname: demo\n---\nv1\n", "skills/demo/notes.md": "one\ntwo\n"})
        self.install(up, "skills/demo", self.agents / "demo")
        self.write_lock({"demo": self.lock_entry(up, "skills/demo")})
        up.commit({"skills/demo/notes.md": "one\ntwo\nthree\n"})
        rc, out, err = self.cli("update", "--yes", "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["results"][0]["outcome"], "updated")
        self.assertEqual((self.agents / "demo" / "notes.md").read_bytes(), b"one\ntwo\nthree\n")


if __name__ == "__main__":
    unittest.main()
