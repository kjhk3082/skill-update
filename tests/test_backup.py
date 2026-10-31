import unittest
from unittest import mock

from helpers import Sandbox

from skillupdate import backup, lockfile, treehash


class BackupSafetyTests(Sandbox):
    def folder(self, name, files):
        d = self.tmp / name
        for rel, text in files.items():
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_text(text)
        return d

    def test_failed_verification_restores_the_old_folder_complete_with_node_modules(self):
        target = self.folder("skill", {"SKILL.md": "old", "node_modules/dep/index.js": "dep"})
        source = self.folder("source", {"SKILL.md": "new"})
        good = treehash.tree_sha(treehash.manifest(str(source)))
        run = backup.Run()
        with mock.patch.object(lockfile, "tree_sha", side_effect=[good, "not-the-hash"]):
            with self.assertRaises(backup.UpdateError):
                lockfile._replace(run, target, source, good, "skill")
        self.assertEqual((target / "SKILL.md").read_text(), "old")
        self.assertEqual((target / "node_modules" / "dep" / "index.js").read_text(), "dep")

    def test_rollback_gives_node_modules_back_to_the_old_folder(self):
        target = self.folder("skill", {"SKILL.md": "old", "node_modules/dep/index.js": "dep"})
        source = self.folder("source", {"SKILL.md": "new"})
        good = treehash.tree_sha(treehash.manifest(str(source)))
        run = backup.Run()
        lockfile._replace(run, target, source, good, "skill")
        run.finish()
        self.assertEqual((target / "SKILL.md").read_text(), "new")
        self.assertTrue((target / "node_modules" / "dep" / "index.js").exists())
        backup.rollback(run.id)
        self.assertEqual((target / "SKILL.md").read_text(), "old")
        self.assertEqual((target / "node_modules" / "dep" / "index.js").read_text(), "dep")

    def test_rollback_recovers_a_run_that_stopped_between_the_two_moves(self):
        target = self.tmp / "skill"                     # the old folder was moved away, the new one never arrived
        saved = self.folder("run-backup", {"SKILL.md": "old"})
        run = backup.Run()
        run.entries.append({"kind": "dir", "target": str(target), "backup": str(saved), "label": "skill", "state": "pending"})
        run.save()
        self.assertEqual(backup.list_runs()[0]["restorable"], 1)
        backup.rollback(run.id)
        self.assertEqual((target / "SKILL.md").read_text(), "old")

    def test_local_state_is_never_moved_through_a_symlink_in_the_new_folder(self):
        old = self.folder("old", {"SKILL.md": "old", "scripts/node_modules/dep/index.js": "dep"})
        outside = self.tmp / "outside"                    # somewhere a hostile upstream tries to aim us at
        outside.mkdir()
        new = self.folder("new", {"SKILL.md": "new"})
        (new / "scripts").symlink_to(outside)
        backup.carry_local_state(old, new)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue((old / "scripts" / "node_modules" / "dep" / "index.js").exists())  # stays in the backup

    def test_rolling_back_twice_does_nothing_the_second_time(self):
        target = self.folder("skill", {"SKILL.md": "old"})
        source = self.folder("source", {"SKILL.md": "new"})
        good = treehash.tree_sha(treehash.manifest(str(source)))
        run = backup.Run()
        lockfile._replace(run, target, source, good, "skill")
        run.finish()
        backup.rollback(run.id)
        self.assertEqual(backup.rollback(run.id), ["nothing to restore in %s" % run.id])
        self.assertEqual((target / "SKILL.md").read_text(), "old")

    def test_a_second_run_cannot_start_while_one_is_in_progress(self):
        with backup.RunLock():
            with self.assertRaises(backup.UpdateError):
                with backup.RunLock():
                    pass


if __name__ == "__main__":
    unittest.main()
