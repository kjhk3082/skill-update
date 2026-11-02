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

from helpers import Sandbox, git

from skillupdate import backup, gitops, report, util
from skillupdate.gitops import RemoteCache, safe_folder, safe_remote, safe_url
from skillupdate.model import KIND_LOCK, OUTDATED, Item


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

    def test_a_link_inside_is_never_followed(self):
        outside = self.tmp / "outside.txt"
        outside.write_text("x")
        outside.chmod(0o644)
        root = self.tmp / "tree"
        (root / "ro").mkdir(parents=True)
        self.symlink(outside, root / "ro" / "link")   # the only entry, so it is the first one that cannot be removed
        (root / "ro").chmod(0o555)
        util.rmtree(root)
        self.assertFalse(root.exists())
        self.assertEqual(outside.stat().st_mode & 0o777, 0o644)   # making things writable stopped at the link


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


class AddressTests(unittest.TestCase):
    def test_a_file_url_names_no_host(self):
        # git for Windows reads file://host/share as the network share //host/share and would connect to it
        self.assertTrue(safe_url("file:///tmp/x.git"))
        self.assertTrue(safe_url("file:///C:/repos/x.git"))
        self.assertFalse(safe_url("file://evil/share/x.git"))
        self.assertFalse(safe_url("file://C:\\repos\\x.git"))

    def test_a_local_path_remote_does_not_start_with_two_slashes(self):
        self.assertTrue(safe_remote("/tmp/x.git"))
        self.assertFalse(safe_remote("//evil/share/x.git"))
        self.assertFalse(safe_remote("\\\\evil\\share\\x.git"))

    def test_folders_that_windows_reads_as_something_else_are_refused_there(self):
        for bad in ("C:/Windows/System32", "D:evil", "a:b", "con", "skills/NUL", "aux.txt", "x.", "a/COM1/b", "lpt9.md"):
            self.assertFalse(safe_folder(bad, windows=True), bad)
        for fine in ("skills/demo", ".claude/skills/x", "a.b/c-d", "console", "nullable", "com10", ""):
            self.assertTrue(safe_folder(fine, windows=True), fine)

    def test_elsewhere_those_names_are_ordinary(self):
        self.assertTrue(safe_folder("skills/con", windows=False))
        self.assertTrue(safe_folder("a:b", windows=False))


class FolderListingTests(Sandbox):
    def test_skill_folders_leave_out_names_windows_would_misread(self):
        up = self.upstream()
        up.commit({"skills/ok/SKILL.md": "x\n", "skills/con/SKILL.md": "x\n"})
        cache = RemoteCache(up.url)
        cache.sync()
        self.assertEqual(sorted(cache.skill_folders()), ["skills/con", "skills/ok"])
        with mock.patch.object(gitops, "_WINDOWS", True):
            self.assertEqual(cache.skill_folders(), ["skills/ok"])


class WhichRulesTests(Sandbox):
    def folders(self):
        a, b = self.tmp / "a", self.tmp / "b"
        a.mkdir()
        b.mkdir()
        return a, b

    @staticmethod
    def make(folder, name):
        p = folder / name
        p.write_text("x")
        p.chmod(0o755)
        return p

    def test_on_windows_a_name_without_an_extension_is_not_a_program(self):
        a, b = self.folders()
        self.make(a, "git")                  # a shell script such as the ones Git ships next to git.exe
        wanted = self.make(b, "git.EXE")     # spelled as PATHEXT spells it: this file system may tell the cases apart
        with mock.patch.dict(os.environ, {"PATH": os.pathsep.join([str(a), str(b)]), "PATHEXT": ".EXE;.CMD"}):
            self.assertEqual(util.which("git", windows=True), str(wanted))

    def test_scripts_can_be_refused(self):
        a, _ = self.folders()
        script = self.make(a, "git.CMD")     # cmd.exe would read special characters in the arguments again
        with mock.patch.dict(os.environ, {"PATH": str(a), "PATHEXT": ".EXE;.CMD"}):
            self.assertEqual(util.which("git", windows=True), str(script))
            self.assertIsNone(util.which("git", windows=True, scripts=False))

    def test_without_git_on_the_path_nothing_else_is_tried(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp)}):
            self.assertIsNone(gitops.git_version())
            self.assertEqual(gitops.git("status").rc, 127)


class ScriptControlTests(Sandbox):
    def test_a_marker_script_really_runs_through_git(self):
        """The negative checks ("this program was never run") only mean something where such a script can run."""
        repo = self.tmp / "repo"
        repo.mkdir()
        git("init", "-q", cwd=repo)
        script, marker = self.marker_script("control")
        git("config", "alias.go", "!" + script.as_posix(), cwd=repo)
        git("go", cwd=repo)
        self.assertTrue(marker.exists())


class SmallThingsTests(Sandbox):
    def test_files_are_written_with_unix_line_endings(self):
        path = self.tmp / "x.json"
        util.atomic_write(path, "a\nb\n")
        self.assertEqual(path.read_bytes(), b"a\nb\n")      # Windows would otherwise write CRLF

    def test_a_byte_order_mark_does_not_hide_the_name(self):
        path = self.tmp / "SKILL.md"
        path.write_bytes(b"\xef\xbb\xbf---\nname: demo\n---\n")     # what Notepad saves
        self.assertEqual(util.frontmatter_name(path), "demo")

    def test_the_python_command_in_messages_follows_the_platform(self):
        item = Item(name="x", kind=KIND_LOCK, status=OUTDATED)
        with mock.patch.object(util, "PYTHON", "py -3"):
            text = report.render_check([item], 1, [], 0.0, False, report.make_style(True))
        self.assertIn("py -3 skill_update.py update", text)    # Windows has no python3, and the Store's stub opens a window


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

    def test_attributes_stored_in_the_repository_do_not_rewrite_the_files(self):
        up = self.upstream()
        up.commit({".gitattributes": "* text eol=crlf\n", "skills/demo/SKILL.md": "---\nname: demo\n---\nv1\n",
                   "skills/demo/notes.md": "one\ntwo\n"})
        self.install(up, "skills/demo", self.agents / "demo")
        self.write_lock({"demo": self.lock_entry(up, "skills/demo")})
        up.commit({"skills/demo/notes.md": "one\ntwo\nthree\n"})
        rc, out, err = self.cli("update", "--yes", "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.agents / "demo" / "notes.md").read_bytes(), b"one\ntwo\nthree\n")


if __name__ == "__main__":
    unittest.main()
