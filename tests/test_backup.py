import json
import os
import unittest
from pathlib import Path
from unittest import mock

from helpers import Sandbox

from skillupdate import backup, lockfile, treehash, util


def tree_of(path):
    return treehash.tree_sha(treehash.manifest(str(path)))


class BackupSafetyTests(Sandbox):
    def folder(self, name, files):
        d = self.tmp / name
        for rel, text in files.items():
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_text(text)
        return d

    def replace(self, run, target, source, label="skill"):
        """lockfile._replace the way an update calls it: expected = hash of the new folder, before = hash of the old."""
        lockfile._replace(run, target, source, tree_of(source), label, tree_of(target))

    def test_failed_verification_restores_the_old_folder_complete_with_node_modules(self):
        target = self.folder("skill", {"SKILL.md": "old", "node_modules/dep/index.js": "dep"})
        source = self.folder("source", {"SKILL.md": "new"})
        good = tree_of(source)
        run = backup.Run()
        # hash calls: the check before the swap, the staged copy, then the installed folder (made to disagree)
        with mock.patch.object(lockfile, "tree_sha", side_effect=["whatever-the-caller-passed", good, "not-the-hash"]):
            with self.assertRaises(backup.UpdateError):
                lockfile._replace(run, target, source, good, "skill", "whatever-the-caller-passed")
        self.assertEqual((target / "SKILL.md").read_text(), "old")
        self.assertEqual((target / "node_modules" / "dep" / "index.js").read_text(), "dep")
        rejected = list((run.dir / "rolled-back").glob("*"))
        self.assertEqual(len(rejected), 1)                              # the rejected folder is set aside, not deleted
        self.assertEqual((rejected[0] / "SKILL.md").read_text(), "new")

    def test_a_folder_edited_after_the_check_is_not_replaced(self):
        target = self.folder("skill", {"SKILL.md": "old"})
        source = self.folder("source", {"SKILL.md": "new"})
        before = tree_of(target)
        (target / "SKILL.md").write_text("edited while the plan was on screen")
        with self.assertRaises(backup.UpdateError):
            lockfile._replace(backup.Run(), target, source, tree_of(source), "skill", before)
        self.assertEqual((target / "SKILL.md").read_text(), "edited while the plan was on screen")

    def test_rollback_gives_node_modules_back_to_the_old_folder(self):
        target = self.folder("skill", {"SKILL.md": "old", "node_modules/dep/index.js": "dep"})
        source = self.folder("source", {"SKILL.md": "new"})
        run = backup.Run()
        self.replace(run, target, source)
        run.finish()
        self.assertEqual((target / "SKILL.md").read_text(), "new")
        self.assertTrue((target / "node_modules" / "dep" / "index.js").exists())
        backup.rollback(run.id)
        self.assertEqual((target / "SKILL.md").read_text(), "old")
        self.assertEqual((target / "node_modules" / "dep" / "index.js").read_text(), "dep")

    def test_rollback_recovers_a_run_that_stopped_between_the_two_moves(self):
        for state in ("away", "pending"):                # both leave the old folder complete in the backup area
            target = self.tmp / ("skill-" + state)       # the new folder never arrived
            run = backup.Run()
            saved = run.dir / "dirs" / "001-skill"
            saved.mkdir(parents=True)
            (saved / "SKILL.md").write_text("old")
            run.entries.append({"kind": "dir", "target": str(target), "backup": str(saved), "label": "skill", "state": state})
            run.save()
            self.assertEqual(backup.list_runs()[-1]["restorable"], 1)
            backup.rollback(run.id)
            self.assertEqual((target / "SKILL.md").read_text(), "old")

    def test_a_pending_entry_without_a_backup_means_nothing_was_moved(self):
        target = self.folder("skill", {"SKILL.md": "untouched"})
        run = backup.Run()
        run.entries.append({"kind": "dir", "target": str(target), "backup": str(run.dir / "dirs" / "001-skill"),
                            "label": "skill", "state": "pending"})
        run.save()
        self.assertEqual(backup.rollback(run.id), ["nothing to restore in %s" % run.id])
        self.assertEqual((target / "SKILL.md").read_text(), "untouched")

    def test_rolling_back_twice_does_nothing_the_second_time(self):
        target = self.folder("skill", {"SKILL.md": "old"})
        source = self.folder("source", {"SKILL.md": "new"})
        run = backup.Run()
        self.replace(run, target, source)
        run.finish()
        backup.rollback(run.id)
        self.assertEqual(backup.rollback(run.id), ["nothing to restore in %s" % run.id])
        self.assertEqual((target / "SKILL.md").read_text(), "old")

    def test_a_skill_on_another_disk_is_refused_never_copied(self):
        target = self.folder("skill", {"SKILL.md": "old"})
        source = self.folder("source", {"SKILL.md": "new"})
        run = backup.Run()
        elsewhere = lambda p: 1 if str(p).startswith(str(target)) else 2
        with mock.patch.object(backup, "_device", side_effect=elsewhere):
            with self.assertRaises(backup.UpdateError) as ctx:
                self.replace(run, target, source)
        self.assertIn("SKILL_UPDATE_HOME", str(ctx.exception))
        self.assertEqual((target / "SKILL.md").read_text(), "old")
        self.assertEqual(run.entries, [])

    def test_a_hostile_lock_key_cannot_steer_the_backup_outside_the_run_folder(self):
        target = self.folder("skill", {"SKILL.md": "old"})
        source = self.folder("source", {"SKILL.md": "new"})
        run = backup.Run()
        self.replace(run, target, source, label="../../../../../escape/..")
        saved = Path(run.entries[0]["backup"])
        self.assertTrue(backup._inside(run.dir, saved), saved)
        self.assertFalse((self.tmp / "escape").exists())

    def test_no_backup_discards_only_after_success_and_never_after_an_error(self):
        for success in (True, False):
            target = self.folder("skill-%s" % success, {"SKILL.md": "old"})
            source = self.folder("source-%s" % success, {"SKILL.md": "new"})
            run = backup.Run(keep_backup=False)
            self.replace(run, target, source)
            saved = Path(run.entries[0]["backup"])
            run.finish(discard=success)
            self.assertEqual(saved.exists(), not success, "success=%s" % success)

    def test_a_run_folder_that_still_holds_backups_is_never_deleted(self):
        run = backup.Run()
        (run.dir / "dirs" / "001-x").mkdir(parents=True)
        (run.dir / "dirs" / "001-x" / "SKILL.md").write_text("the only copy")
        run.finish()
        self.assertTrue((run.dir / "dirs" / "001-x" / "SKILL.md").exists())

    def test_local_state_is_never_moved_through_a_symlink_in_the_new_folder(self):
        old = self.folder("old", {"SKILL.md": "old", "scripts/node_modules/dep/index.js": "dep"})
        outside = self.tmp / "outside"                    # somewhere a hostile upstream tries to aim us at
        outside.mkdir()
        new = self.folder("new", {"SKILL.md": "new"})
        (new / "scripts").symlink_to(outside)
        backup.carry_local_state(old, new)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue((old / "scripts" / "node_modules" / "dep" / "index.js").exists())  # stays in the backup

    def test_lock_rollback_undoes_only_what_the_run_changed(self):
        path = self.write_lock({"a": {"skillFolderHash": "new-a", "updatedAt": "t1"}, "b": {"skillFolderHash": "b"}})
        run = backup.Run()
        run.record_lock(path, before={"a": {"skillFolderHash": "old-a", "updatedAt": None}},
                        after={"a": {"skillFolderHash": "new-a", "updatedAt": "t1"}})
        data = self.read_lock()                           # meanwhile `npx skills add` added a skill
        data["skills"]["c"] = {"skillFolderHash": "c"}
        path.write_text(json.dumps(data))
        backup.rollback(run.id)
        skills = self.read_lock()["skills"]
        self.assertEqual(skills["a"], {"skillFolderHash": "old-a"})   # restored; the field that did not exist is gone
        self.assertEqual(skills["b"], {"skillFolderHash": "b"})
        self.assertEqual(skills["c"], {"skillFolderHash": "c"})       # not clobbered by an old snapshot

    def test_lock_rollback_leaves_an_entry_that_changed_again_alone(self):
        path = self.write_lock({"a": {"skillFolderHash": "changed-since"}})
        run = backup.Run()
        run.record_lock(path, before={"a": {"skillFolderHash": "old"}}, after={"a": {"skillFolderHash": "new"}})
        lines = backup.rollback(run.id)
        self.assertTrue(any("changed since" in ln for ln in lines))
        self.assertEqual(self.read_lock()["skills"]["a"]["skillFolderHash"], "changed-since")

    def test_writing_through_a_symlinked_file_keeps_the_link(self):
        real = self.tmp / "dotfiles" / "lock.json"
        real.parent.mkdir()
        real.write_text("{}")
        link = self.tmp / "lock-link.json"
        link.symlink_to(real)
        util.atomic_write(link, '{"x": 1}')
        self.assertTrue(link.is_symlink())
        self.assertEqual(json.loads(real.read_text()), {"x": 1})

    def test_a_second_run_cannot_start_while_one_is_in_progress(self):
        with backup.RunLock():
            with self.assertRaises(backup.UpdateError):
                with backup.RunLock():
                    pass


if __name__ == "__main__":
    unittest.main()
