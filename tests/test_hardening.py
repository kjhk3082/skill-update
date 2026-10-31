"""Scenarios found by reviewing the tool as an attacker, as a careless user and as a crash would."""
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from helpers import Sandbox, git

from skillupdate import backup, gitskill, util
from skillupdate.model import KIND_GIT, OUTDATED, Item

SKILL = "---\nname: %s\n---\n%s\n"


class GitTrustTests(Sandbox):
    def pack(self):
        up = self.upstream()
        up.commit({"SKILL.md": SKILL % ("pack", "v1"), "lib/a.txt": "a\n"})
        return up, self.clone(up)

    def test_unknown_keys_in_a_clones_own_config_make_it_untrusted(self):
        up, repo = self.pack()
        script, marker = self.marker_script("x")
        for key, value in (("url.http://evil/.insteadOf", "http://good/"), ("protocol.ext.allow", "always"),
                           ("gpg.program", str(script)), ("core.alternateRefsCommand", str(script)),
                           ("include.path", str(self.tmp / "extra.cfg")), ("credential.helper", "!" + str(script)),
                           ("alias.st", "!" + str(script)), ("core.sshCommand", str(script))):
            git("config", key, value, cwd=repo)
            it = self.item(self.check(), "pack")
            self.assertEqual(it["status"], "blocked", key)
            self.assertIn("does not trust", it["notes"][0], key)
            git("config", "--unset-all", key, cwd=repo)
        self.assertEqual(self.item(self.check(), "pack")["status"], "current")
        self.assertFalse(marker.exists())

    def test_settings_in_the_worktree_scope_are_inspected_too(self):
        up, repo = self.pack()
        script, marker = self.marker_script("fsmonitor")
        git("config", "extensions.worktreeConfig", "true", cwd=repo)
        git("config", "--worktree", "core.fsmonitor", str(script), cwd=repo)
        it = self.item(self.check(), "pack")
        self.assertEqual(it["status"], "blocked")
        self.assertIn("core.fsmonitor", it["notes"][0])
        self.assertFalse(marker.exists())

    def test_ordinary_user_settings_do_not_get_in_the_way(self):
        up, repo = self.pack()
        for key, value in (("user.name", "Someone"), ("user.email", "s@example.com"), ("branch.main.rebase", "true"),
                           ("pull.ff", "only"), ("core.autocrlf", "false"), ("fetch.prune", "true")):
            git("config", key, value, cwd=repo)
        up.commit({"SKILL.md": SKILL % ("pack", "v2")})
        self.assertEqual(self.item(self.check(), "pack")["status"], "outdated")
        self.assertEqual(self.cli("update", "--yes")[0], 0)
        self.assertIn("v2", (repo / "SKILL.md").read_text())

    def test_a_remote_that_names_a_helper_program_is_never_fetched(self):
        up, repo = self.pack()
        script, marker = self.marker_script("ext")
        git("remote", "set-url", "origin", "ext::sh -c %s" % script, cwd=repo)
        it = self.item(self.check(), "pack")
        self.assertEqual(it["status"], "blocked")
        self.assertIn("not an ordinary", it["notes"][0])
        self.assertFalse(marker.exists())

    def test_rollback_keeps_edits_made_after_the_update_in_a_stash(self):
        up, repo = self.pack()
        old = git("rev-parse", "HEAD", cwd=repo)
        up.commit({"SKILL.md": SKILL % ("pack", "v2")})
        run_id = json.loads(self.cli("update", "--yes", "--json")[1])["run_id"]
        (repo / "lib" / "a.txt").write_text("edited after the update\n")
        rc, out, err = self.cli("rollback", run_id)
        self.assertEqual(rc, 0, err)
        self.assertIn("git stash", out)
        self.assertEqual(git("rev-parse", "HEAD", cwd=repo), old)
        self.assertEqual((repo / "lib" / "a.txt").read_text(), "a\n")
        self.assertIn("skill-update rollback", git("stash", "list", cwd=repo))

    def pull_item(self, repo):
        return Item(name="pack", kind=KIND_GIT, status=OUTDATED, data={"repo": str(repo), "dirty": True, "behind": 1})

    def test_an_interruption_after_stashing_is_recorded_and_rollback_gives_the_edits_back(self):
        up, repo = self.pack()
        (repo / "lib" / "a.txt").write_text("my edit\n")
        up.commit({"SKILL.md": SKILL % ("pack", "v2")})
        git("fetch", "-q", cwd=repo)
        real = gitskill.git

        def interrupted(*args, **kwargs):
            if args and args[0] == "merge":
                raise KeyboardInterrupt
            return real(*args, **kwargs)

        run = backup.Run()
        with mock.patch.object(gitskill, "git", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            gitskill._pull(run, self.pull_item(repo))
        self.assertEqual((repo / "lib" / "a.txt").read_text(), "a\n")           # the edit is out of the way...
        self.assertEqual([e["state"] for e in run.entries], ["stashed"])         # ...and written down
        backup.rollback(run.id)
        self.assertEqual((repo / "lib" / "a.txt").read_text(), "my edit\n")

    def test_a_failed_merge_puts_the_edits_straight_back(self):
        up, repo = self.pack()
        (repo / "lib" / "a.txt").write_text("my edit\n")
        up.commit({"SKILL.md": SKILL % ("pack", "v2")})
        git("fetch", "-q", cwd=repo)
        real = gitskill.git

        def failing(*args, **kwargs):
            if args and args[0] == "merge":
                return util.Proc(1, b"", b"fatal: simulated failure")
            return real(*args, **kwargs)

        with mock.patch.object(gitskill, "git", side_effect=failing):
            result = gitskill._pull(backup.Run(), self.pull_item(repo))
        self.assertEqual(result.outcome, "failed")
        self.assertEqual((repo / "lib" / "a.txt").read_text(), "my edit\n")
        self.assertEqual(git("stash", "list", cwd=repo), "")                     # nothing left behind in the stash


class LockHardeningTests(Sandbox):
    def test_a_git_clone_is_left_to_the_git_channel_even_when_the_lock_names_it(self):
        up = self.upstream()
        up.commit({"SKILL.md": SKILL % ("pack", "v1"), "skills/pack/SKILL.md": SKILL % ("pack", "v1")})
        self.write_lock({"pack": self.lock_entry(up, "skills/pack")})
        repo = self.clone(up, "pack")                       # same name as the lock entry, but it is a clone
        up.commit({"SKILL.md": SKILL % ("pack", "v2"), "skills/pack/SKILL.md": SKILL % ("pack", "v2")})
        it = self.item(self.check(), "pack")
        self.assertEqual(it["kind"], "git")
        self.assertTrue((repo / ".git").is_dir())
        self.cli("update", "--yes")
        self.assertTrue((repo / ".git").is_dir())           # not swapped for a plain copy

    def test_a_lock_entry_without_a_path_is_unknown_not_the_whole_repository(self):
        up = self.upstream()
        up.commit({"SKILL.md": SKILL % ("whole", "repo"), "other/file.md": "x\n"})
        entry = self.lock_entry(up, "")
        del entry["skillPath"]
        self.install(up, "", self.agents / "whole")
        self.write_lock({"whole": entry})
        it = self.item(self.check(), "whole")
        self.assertEqual(it["status"], "unknown")
        self.assertTrue(any("no path" in n for n in it["notes"]))

    def test_the_lock_file_is_found_the_way_the_cli_finds_it(self):
        up = self.upstream()
        up.commit({"skills/demo/SKILL.md": SKILL % ("demo", "v1")})
        self.install(up, "skills/demo", self.agents / "demo")
        self.write_lock({"demo": self.lock_entry(up, "skills/demo", tree="0" * 40)})          # the stale one in ~/.agents
        xdg = self.tmp / "xdg"
        (xdg / "skills").mkdir(parents=True)
        (xdg / "skills" / ".skill-lock.json").write_text(json.dumps(
            {"version": 3, "skills": {"demo": self.lock_entry(up, "skills/demo")}}))        # the one XDG_STATE_HOME points at
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(xdg)}):
            self.assertEqual(self.item(self.check(), "demo")["status"], "current")

    def test_the_lock_file_keeps_being_a_symlink_after_an_update(self):
        up = self.upstream()
        up.commit({"skills/demo/SKILL.md": SKILL % ("demo", "v1")})
        self.install(up, "skills/demo", self.agents / "demo")
        real = self.write_lock({"demo": self.lock_entry(up, "skills/demo")})
        stored = self.tmp / "dotfiles" / "skill-lock.json"
        stored.parent.mkdir()
        stored.write_text(real.read_text())
        real.unlink()
        real.symlink_to(stored)
        up.commit({"skills/demo/SKILL.md": SKILL % ("demo", "v2")})
        self.cli("update", "--yes")
        self.assertTrue(real.is_symlink())
        self.assertEqual(json.loads(stored.read_text())["skills"]["demo"]["skillFolderHash"], up.tree("skills/demo"))

    def test_a_clean_cli_install_that_omits_metadata_json_is_current_not_edited(self):
        up = self.upstream()
        files = {"skills/demo/SKILL.md": SKILL % ("demo", "v1"), "skills/demo/metadata.json": '{"version": "1"}\n',
                 "skills/demo/nested/metadata.json": "{}\n", "skills/demo/ref.md": "ref\n"}
        up.commit(files)
        self.install(up, "skills/demo", self.agents / "demo")
        (self.agents / "demo" / "metadata.json").unlink()                       # what `npx skills add` leaves out
        (self.agents / "demo" / "nested" / "metadata.json").unlink()
        self.write_lock({"demo": self.lock_entry(up, "skills/demo")})
        self.assertEqual(self.item(self.check(), "demo")["status"], "current")

        up.commit({"skills/demo/SKILL.md": SKILL % ("demo", "v2")})
        it = self.item(self.check(), "demo")
        self.assertEqual(it["status"], "outdated")
        self.assertFalse(it["modified"])                                         # an omission is not an edit

        rc, out, err = self.cli("update", "--yes", "--json")
        self.assertEqual(rc, 0, err)
        self.assertTrue((self.agents / "demo" / "metadata.json").exists())      # the folder now matches upstream exactly
        self.assertEqual(self.item(self.check(), "demo")["status"], "current")

    def test_junk_committed_upstream_does_not_make_an_update_fail_forever(self):
        up = self.upstream()
        up.commit({"skills/demo/SKILL.md": SKILL % ("demo", "v1"), "skills/demo/.DS_Store": "junk",
                   "skills/demo/__pycache__/m.pyc": "junk", "skills/demo/node_modules/x/index.js": "junk"})
        self.install(up, "skills/demo", self.agents / "demo")
        self.write_lock({"demo": self.lock_entry(up, "skills/demo")})
        self.assertEqual(self.item(self.check(), "demo")["status"], "current")
        up.commit({"skills/demo/SKILL.md": SKILL % ("demo", "v2")})
        rc, out, err = self.cli("update", "--yes", "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["results"][0]["outcome"], "updated")

    def test_a_lock_key_with_slashes_updates_without_leaving_the_backup_area(self):
        up = self.upstream()
        up.commit({"skills/x/SKILL.md": SKILL % ("x", "v1")})
        dirname = "x-..-y"                                       # what the CLI makes of the name `x/../y`
        self.install(up, "skills/x", self.agents / dirname)
        self.write_lock({"x/../y": self.lock_entry(up, "skills/x")})
        up.commit({"skills/x/SKILL.md": SKILL % ("x", "v2")})
        rc, out, err = self.cli("update", "--yes", "--json")
        self.assertEqual(rc, 0, err)
        self.assertIn("v2", (self.agents / dirname / "SKILL.md").read_text())
        state = self.tmp / "state"
        for path in state.rglob("*"):
            self.assertTrue(str(path.resolve()).startswith(str(state.resolve())), path)
        self.assertIn("x/../y", self.read_lock()["skills"])      # the real key is what gets updated

    def test_a_stale_lock_record_needs_a_yes_like_everything_else_that_writes(self):
        up = self.upstream()
        up.commit({"skills/demo/SKILL.md": SKILL % ("demo", "v1")})
        old_tree = up.tree("skills/demo")
        up.commit({"skills/demo/SKILL.md": SKILL % ("demo", "v2")})
        self.install(up, "skills/demo", self.agents / "demo")
        self.write_lock({"demo": self.lock_entry(up, "skills/demo", tree=old_tree)})
        rc, _out, err = self.cli("update")
        self.assertEqual(rc, 2)
        self.assertIn("--yes", err)
        self.assertEqual(self.read_lock()["skills"]["demo"]["skillFolderHash"], old_tree)

    def test_no_backup_is_refused_while_a_skill_has_local_edits(self):
        up = self.upstream()
        up.commit({"skills/demo/SKILL.md": SKILL % ("demo", "v1"), "skills/demo/keep.md": "keep\n"})
        self.install(up, "skills/demo", self.agents / "demo")
        self.write_lock({"demo": self.lock_entry(up, "skills/demo")})
        (self.agents / "demo" / "keep.md").write_text("my edit\n")
        up.commit({"skills/demo/SKILL.md": SKILL % ("demo", "v2")})
        rc, _out, err = self.cli("update", "--yes", "--no-backup")
        self.assertEqual(rc, 2)
        self.assertIn("local edits", err)
        self.assertEqual((self.agents / "demo" / "keep.md").read_text(), "my edit\n")


if __name__ == "__main__":
    unittest.main()
