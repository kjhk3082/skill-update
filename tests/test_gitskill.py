import json
import os
import unittest

from helpers import Sandbox, git


class GitChannelTests(Sandbox):
    def clone(self, up, name="pack"):
        self.claude.mkdir(parents=True, exist_ok=True)
        dest = self.claude / name
        git("clone", "-q", up.url, str(dest))
        return dest

    def pack(self):
        up = self.upstream()
        up.commit({"SKILL.md": "---\nname: pack\n---\nv1\n", "lib/a.txt": "a\n"})
        return up, self.clone(up)

    def test_up_to_date_clone_is_current(self):
        self.pack()
        it = self.item(self.check(), "pack")
        self.assertEqual((it["kind"], it["status"]), ("git", "current"))

    def test_behind_clone_is_outdated_then_fast_forwarded(self):
        up, repo = self.pack()
        up.commit({"SKILL.md": "---\nname: pack\n---\nv2\n", "lib/b.txt": "b\n"}, "second change")
        it = self.item(self.check(), "pack")
        self.assertEqual(it["status"], "outdated")
        self.assertEqual(it["commits"], ["second change"])
        self.assertEqual(it["changes"]["added"], 1)
        rc, out, err = self.cli("update", "--yes", "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["results"][0]["outcome"], "updated")
        self.assertIn("v2", (repo / "SKILL.md").read_text())
        self.assertEqual(self.item(self.check(), "pack")["status"], "current")

    def test_uncommitted_edits_are_stashed_never_lost(self):
        up, repo = self.pack()
        (repo / "lib" / "a.txt").write_text("my edit\n")
        (repo / "notes.txt").write_text("untracked\n")
        up.commit({"SKILL.md": "---\nname: pack\n---\nv2\n"})
        self.assertTrue(self.item(self.check(), "pack")["modified"])
        rc, out, _ = self.cli("update", "--yes", "--json")
        self.assertEqual(rc, 0)
        self.assertIn("v2", (repo / "SKILL.md").read_text())
        self.assertEqual((repo / "lib" / "a.txt").read_text(), "a\n")      # working tree matches upstream again
        stash = git("stash", "list", cwd=repo)
        self.assertIn("skill-update", stash)                              # ...and the edits are safe in the stash
        self.assertIn("git stash", " ".join(json.loads(out)["results"][0]["notes"]))
        git("stash", "pop", "-q", cwd=repo)
        self.assertEqual((repo / "lib" / "a.txt").read_text(), "my edit\n")
        self.assertEqual((repo / "notes.txt").read_text(), "untracked\n")

    def test_diverged_clone_is_blocked_and_untouched(self):
        up, repo = self.pack()
        (repo / "mine.txt").write_text("local commit\n")
        git("add", "-A", cwd=repo)
        git("commit", "-q", "-m", "local", cwd=repo)
        head = git("rev-parse", "HEAD", cwd=repo)
        up.commit({"SKILL.md": "---\nname: pack\n---\nv2\n"})
        it = self.item(self.check(), "pack")
        self.assertEqual(it["status"], "blocked")
        self.assertIn("diverged", it["notes"][0])
        self.cli("update", "--yes")
        self.assertEqual(git("rev-parse", "HEAD", cwd=repo), head)

    def test_clone_without_a_remote_is_unmanaged(self):
        self.claude.mkdir(parents=True)
        repo = self.claude / "solo"
        repo.mkdir()
        (repo / "SKILL.md").write_text("---\nname: solo\n---\n")
        git("init", "-q", "-b", "main", cwd=repo)
        it = self.item(self.check(), "solo")
        self.assertEqual(it["status"], "unmanaged")
        self.assertIn("without a remote", it["notes"][0])

    def test_skill_in_somebody_elses_repository_is_never_fetched(self):
        up = self.upstream()
        up.commit({"skills/linked/SKILL.md": "---\nname: linked\n---\n"})
        work = self.home / "projects" / "mine"
        git("clone", "-q", up.url, str(work))
        self.claude.mkdir(parents=True)
        os.symlink(work / "skills" / "linked", self.claude / "linked")
        up.commit({"skills/linked/SKILL.md": "---\nname: linked\n---\nnewer\n"})
        head = git("rev-parse", "HEAD", cwd=work)
        it = self.item(self.check(), "linked")
        self.assertEqual(it["status"], "unmanaged")
        self.assertIn("outside your skill folders", it["notes"][0])
        self.cli("update", "--yes")
        self.assertEqual(git("rev-parse", "HEAD", cwd=work), head)

    def marker_script(self, name):
        marker = self.tmp / (name + ".ran")
        script = self.tmp / (name + ".sh")
        script.write_text("#!/bin/sh\ntouch %s\n" % marker)
        script.chmod(0o755)
        return script, marker

    def test_a_clone_whose_config_can_run_programs_is_left_alone(self):
        up, repo = self.pack()
        for key in ("core.fsmonitor", "core.sshCommand", "credential.helper", "remote.origin.uploadpack"):
            script, marker = self.marker_script(key.replace(".", "-"))
            git("config", key, str(script), cwd=repo)
        up.commit({"SKILL.md": "---\nname: pack\n---\nv2\n"})
        it = self.item(self.check(), "pack")
        self.assertEqual(it["status"], "blocked")
        self.assertIn("does not trust", it["notes"][0])
        self.cli("update", "--yes")
        self.assertIn("v1", (repo / "SKILL.md").read_text())
        self.assertEqual(list(self.tmp.glob("*.ran")), [])

    def test_hooks_inside_a_cloned_skill_never_run(self):
        up, repo = self.pack()
        for hook in ("reference-transaction", "post-merge", "pre-auto-gc", "post-checkout"):
            script, _marker = self.marker_script(hook)
            (repo / ".git" / "hooks" / hook).write_text(script.read_text())
            (repo / ".git" / "hooks" / hook).chmod(0o755)
        up.commit({"SKILL.md": "---\nname: pack\n---\nv2\n"})
        self.cli("check")
        rc, out, err = self.cli("update", "--yes", "--json")
        self.assertEqual(rc, 0, err)
        self.assertIn("v2", (repo / "SKILL.md").read_text())   # the update itself still worked
        self.assertEqual(list(self.tmp.glob("*.ran")), [])

    def test_a_git_file_instead_of_a_git_folder_is_not_managed(self):
        self.claude.mkdir(parents=True)
        d = self.claude / "linked-worktree"
        d.mkdir()
        (d / "SKILL.md").write_text("---\nname: linked-worktree\n---\n")
        (d / ".git").write_text("gitdir: /somewhere/else\n")
        it = self.item(self.check(), "linked-worktree")
        self.assertEqual(it["status"], "blocked")
        self.assertIn("not a plain folder", it["notes"][0])

    def test_setup_script_is_reported_but_never_run(self):
        up, repo = self.pack()
        up.commit({"setup": ("#!/bin/sh\ntouch %s/ran\n" % self.tmp, 0o755), "SKILL.md": "---\nname: pack\n---\nv2\n"})
        it = self.item(self.check(), "pack")
        self.assertTrue(any("setup script" in n for n in it["notes"]))
        self.cli("update", "--yes")
        self.assertTrue((repo / "setup").exists())
        self.assertFalse((self.tmp / "ran").exists())

    def test_rollback_returns_to_the_old_commit_and_gives_the_edits_back(self):
        up, repo = self.pack()
        (repo / "lib" / "a.txt").write_text("my edit\n")
        old = git("rev-parse", "HEAD", cwd=repo)
        up.commit({"SKILL.md": "---\nname: pack\n---\nv2\n"})
        run_id = json.loads(self.cli("update", "--yes", "--json")[1])["run_id"]
        self.assertNotEqual(git("rev-parse", "HEAD", cwd=repo), old)
        rc, _out, err = self.cli("rollback", run_id)
        self.assertEqual(rc, 0, err)
        self.assertEqual(git("rev-parse", "HEAD", cwd=repo), old)
        self.assertEqual((repo / "lib" / "a.txt").read_text(), "my edit\n")


if __name__ == "__main__":
    unittest.main()
