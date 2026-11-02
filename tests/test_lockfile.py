import json
import os
import unittest

from helpers import Sandbox


def v1(name="demo"):
    return {"skills/%s/SKILL.md" % name: "---\nname: %s\n---\nv1\n" % name,
            "skills/%s/old.md" % name: "old\n", "skills/%s/keep.md" % name: "keep\n"}


def v2(name="demo"):
    return {"skills/%s/SKILL.md" % name: "---\nname: %s\n---\nv2\n" % name,
            "skills/%s/old.md" % name: None, "skills/%s/new.md" % name: "new\n"}


class LockChannelTests(Sandbox):
    def setup_skill(self, name="demo", key=None):
        up = self.upstream()
        up.commit(v1(name))
        folder = "skills/%s" % name
        self.install(up, folder, self.agents / name)
        self.write_lock({key or name: self.lock_entry(up, folder)})
        return up, folder

    def test_identical_folder_is_current(self):
        self.setup_skill()
        self.assertEqual(self.item(self.check(), "demo")["status"], "current")

    def test_pristine_older_folder_is_outdated_with_a_change_summary(self):
        up, _ = self.setup_skill()
        up.commit(v2())
        it = self.item(self.check(), "demo")
        self.assertEqual((it["status"], it["modified"]), ("outdated", False))
        self.assertEqual({k: it["changes"][k] for k in ("added", "modified", "removed")}, {"added": 1, "modified": 1, "removed": 1})
        self.assertEqual(it["agents"], ["universal"])

    def test_local_edit_plus_new_upstream_is_flagged_modified(self):
        up, _ = self.setup_skill()
        (self.agents / "demo" / "keep.md").write_text("my edit\n")
        up.commit(v2())
        it = self.item(self.check(), "demo")
        self.assertEqual((it["status"], it["modified"]), ("outdated", True))

    def test_local_edit_with_unchanged_upstream_needs_no_update(self):
        self.setup_skill()
        (self.agents / "demo" / "keep.md").write_text("my edit\n")
        it = self.item(self.check(), "demo")
        self.assertEqual(it["status"], "current")
        self.assertTrue(any("local edits" in n for n in it["notes"]))

    def test_folder_renamed_upstream_is_found_again(self):
        up, _ = self.setup_skill()
        up.commit({"skills/demo": None, "tools/renamed-demo/SKILL.md": "---\nname: demo\n---\nv2\n"})
        it = self.item(self.check(), "demo")
        self.assertEqual(it["status"], "outdated")
        self.assertTrue(any("moved upstream" in n for n in it["notes"]))

    def test_skill_removed_upstream_is_unknown(self):
        up, _ = self.setup_skill()
        up.commit({"skills/demo": None, "other/SKILL.md": "---\nname: other\n---\n"})
        it = self.item(self.check(), "demo")
        self.assertEqual(it["status"], "unknown")
        self.assertTrue(any("no longer contains" in n for n in it["notes"]))

    def test_unreachable_source_is_an_error_not_a_crash(self):
        up, _ = self.setup_skill()
        entry = self.lock_entry(up, "skills/demo")
        entry["sourceUrl"] = "file://" + str(self.tmp / "does-not-exist.git")
        self.write_lock({"demo": entry})
        self.assertEqual(self.item(self.check(), "demo")["status"], "error")

    def test_same_name_in_another_skill_is_left_alone(self):
        up, _ = self.setup_skill()
        up.commit(v2())
        (self.agents / "demo" / "SKILL.md").write_text("---\nname: something-else\n---\n")
        it = self.item(self.check(), "demo")
        self.assertEqual(it["status"], "blocked")

    def test_update_replaces_folder_refreshes_lock_and_can_be_rolled_back(self):
        up, folder = self.setup_skill()
        before = (self.agents / "demo" / "SKILL.md").read_text()
        up.commit(v2())
        rc, out, err = self.cli("update", "--yes", "--json")
        self.assertEqual(rc, 0, err)
        data = json.loads(out)
        self.assertEqual([r["outcome"] for r in data["results"]], ["updated"])
        self.assertIn("v2", (self.agents / "demo" / "SKILL.md").read_text())
        self.assertFalse((self.agents / "demo" / "old.md").exists())
        self.assertEqual(self.read_lock()["skills"]["demo"]["skillFolderHash"], up.tree(folder))
        self.assertEqual(self.item(self.check(), "demo")["status"], "current")  # verified against upstream

        rc, out, err = self.cli("rollback", data["run_id"])
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.agents / "demo" / "SKILL.md").read_text(), before)
        self.assertTrue((self.agents / "demo" / "old.md").exists())

    def test_update_needs_yes_when_there_is_no_terminal(self):
        up, _ = self.setup_skill()
        up.commit(v2())
        rc, _out, err = self.cli("update")
        self.assertEqual(rc, 2)
        self.assertIn("--yes", err)
        self.assertIn("v1", (self.agents / "demo" / "SKILL.md").read_text())

    def test_dry_run_changes_nothing(self):
        up, _ = self.setup_skill()
        up.commit(v2())
        rc, out, _ = self.cli("update", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("Dry run", out)
        self.assertIn("v1", (self.agents / "demo" / "SKILL.md").read_text())
        self.assertFalse((self.tmp / "state" / "backups").exists())

    def test_keep_modified_skips_but_a_named_skill_is_still_updated(self):
        up, _ = self.setup_skill()
        (self.agents / "demo" / "keep.md").write_text("my edit\n")
        up.commit(v2())
        rc, out, _ = self.cli("update", "--yes", "--keep-modified", "--json")
        self.assertEqual(json.loads(out)["skipped"][0]["name"], "demo")
        self.assertIn("v1", (self.agents / "demo" / "SKILL.md").read_text())
        rc, out, _ = self.cli("update", "demo", "--yes", "--keep-modified", "--json")
        self.assertIn("v2", (self.agents / "demo" / "SKILL.md").read_text())

    def test_symlinked_exposure_is_one_skill_and_follows_the_update(self):
        up, _ = self.setup_skill()
        self.claude.mkdir(parents=True)
        self.symlink(self.agents / "demo", self.claude / "demo")
        it = self.item(self.check(), "demo")
        self.assertEqual(it["agents"], ["claude-code", "universal"])
        up.commit(v2())
        self.cli("update", "--yes")
        self.assertTrue((self.claude / "demo").is_symlink())
        self.assertIn("v2", (self.claude / "demo" / "SKILL.md").read_text())

    def test_two_lock_entries_with_one_folder_name_follow_their_own_upstream_folder(self):
        # a repository that publishes the same skill twice: a generic copy and a Claude-specific one
        up = self.upstream()
        up.commit({"skills/dup-x/SKILL.md": "generic v1\n", ".claude/skills/dup-x/SKILL.md": "claude v1\n"})
        self.install(up, "skills/dup-x", self.agents / "dup-x")
        self.install(up, ".claude/skills/dup-x", self.claude / "dup-x")
        # `dup:x` and `dup-x` both become the folder name `dup-x`
        self.write_lock({"dup-x": self.lock_entry(up, "skills/dup-x"), "dup:x": self.lock_entry(up, ".claude/skills/dup-x")})
        up.commit({"skills/dup-x/SKILL.md": "generic v2\n", ".claude/skills/dup-x/SKILL.md": "claude v2\n"})
        data = self.check()
        self.assertEqual({i["name"]: i["status"] for i in data["items"]}, {"dup-x": "outdated", "dup:x": "outdated"})
        self.assertFalse(any(i["modified"] for i in data["items"]))
        self.cli("update", "--yes")
        self.assertEqual((self.agents / "dup-x" / "SKILL.md").read_text(), "generic v2\n")
        self.assertEqual((self.claude / "dup-x" / "SKILL.md").read_text(), "claude v2\n")

    def test_a_lock_path_written_with_backslashes_still_names_just_that_folder(self):
        up, _ = self.setup_skill()
        entry = self.read_lock()["skills"]["demo"]
        entry["skillPath"] = "skills\\demo\\SKILL.md"   # lock files written on Windows may look like this
        self.write_lock({"demo": entry})
        self.assertEqual(self.item(self.check(), "demo")["status"], "current")
        up.commit(v2())
        rc, out, err = self.cli("update", "--yes", "--json")
        self.assertEqual(rc, 0, err)
        self.assertIn("v2", (self.agents / "demo" / "SKILL.md").read_text())
        self.assertFalse((self.agents / "demo" / "skills").exists())   # the whole repository was not put into the skill

    def test_node_modules_survive_an_update(self):
        up, _ = self.setup_skill()
        (self.agents / "demo" / "node_modules" / "dep").mkdir(parents=True)
        (self.agents / "demo" / "node_modules" / "dep" / "index.js").write_text("module.exports = 1")
        up.commit(v2())
        self.cli("update", "--yes")
        self.assertTrue((self.agents / "demo" / "node_modules" / "dep" / "index.js").exists())
        self.assertIn("v2", (self.agents / "demo" / "SKILL.md").read_text())

    def test_stale_lock_record_is_refreshed_when_the_files_are_already_current(self):
        up = self.upstream()
        up.commit(v1())
        old_tree = up.tree("skills/demo")
        up.commit(v2())
        self.install(up, "skills/demo", self.agents / "demo")          # files are already the new version
        self.write_lock({"demo": self.lock_entry(up, "skills/demo", tree=old_tree)})  # but the lock says old
        self.assertEqual(self.item(self.check(), "demo")["status"], "current")
        self.cli("update", "--yes")
        self.assertEqual(self.read_lock()["skills"]["demo"]["skillFolderHash"], up.tree("skills/demo"))

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                     "needs permission bits that root and Windows ignore")
    def test_a_file_that_cannot_be_read_blocks_that_skill_only(self):
        up, _ = self.setup_skill()
        up.commit(v2())
        other = self.upstream("other")
        other.commit({"skills/fine/SKILL.md": "---\nname: fine\n---\nv1\n"})
        self.install(other, "skills/fine", self.agents / "fine")
        self.write_lock({"demo": self.read_lock()["skills"]["demo"], "fine": self.lock_entry(other, "skills/fine")})
        secret = self.agents / "demo" / "keep.md"
        secret.chmod(0)
        self.addCleanup(secret.chmod, 0o644)
        data = self.check()
        self.assertEqual(self.item(data, "demo")["status"], "blocked")
        self.assertTrue(any("could not be read" in n for n in self.item(data, "demo")["notes"]))
        self.assertEqual(self.item(data, "fine")["status"], "current")

    def test_hostile_lock_values_never_reach_git(self):
        up, _ = self.setup_skill()
        marker = self.tmp / "pwned"
        evil_url = self.lock_entry(up, "skills/demo")
        evil_url["sourceUrl"] = "--upload-pack=touch %s" % marker
        evil_path = self.lock_entry(up, "skills/demo")
        evil_path["skillPath"] = "../../../etc/SKILL.md"
        evil_ref = self.lock_entry(up, "skills/demo")
        evil_ref["ref"] = "--config=core.sshCommand=touch %s" % marker
        for name, entry in (("evil-url", evil_url), ("evil-path", evil_path), ("evil-ref", evil_ref)):
            self.install(up, "skills/demo", self.agents / name)
            (self.agents / name / "SKILL.md").write_text("---\nname: %s\n---\n" % name)
        self.write_lock({"evil-url": evil_url, "evil-path": evil_path, "evil-ref": evil_ref})
        data = self.check()
        self.assertEqual({i["name"]: i["status"] for i in data["items"] if i["name"].startswith("evil")},
                         {"evil-url": "error", "evil-path": "unknown", "evil-ref": "error"})
        self.assertFalse(marker.exists())

    def test_unsupported_lock_version_is_never_written(self):
        up, _ = self.setup_skill()
        path = self.home / ".agents" / ".skill-lock.json"
        data = json.loads(path.read_text())
        data["version"] = 99
        path.write_text(json.dumps(data))
        up.commit(v2())
        self.cli("update", "--yes")
        self.assertEqual(json.loads(path.read_text())["version"], 99)
        self.assertEqual(json.loads(path.read_text())["skills"]["demo"]["skillFolderHash"], data["skills"]["demo"]["skillFolderHash"])


if __name__ == "__main__":
    unittest.main()
