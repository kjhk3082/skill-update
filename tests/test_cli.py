import json
import os
import unittest

from helpers import Sandbox, SCRIPTS

SKILL = "---\nname: %s\n---\nv%d\n"


class CliTests(Sandbox):
    def stale_skill(self, name="demo", source="acme/demo-repo"):
        up = self.upstream()
        up.commit({"skills/%s/SKILL.md" % name: SKILL % (name, 1)})
        self.install(up, "skills/" + name, self.agents / name)
        self.write_lock({name: self.lock_entry(up, "skills/" + name, source=source)})
        up.commit({"skills/%s/SKILL.md" % name: SKILL % (name, 2)})
        return up

    def test_no_arguments_means_check(self):
        self.stale_skill()
        rc, out, _ = self.cli()
        self.assertEqual(rc, 0)
        self.assertIn("Updates available (1)", out)

    def test_json_report_has_a_stable_shape(self):
        self.stale_skill()
        data = self.check()
        self.assertEqual(data["tool"], "skill-update")
        self.assertEqual(data["command"], "check")
        self.assertEqual(data["summary"]["outdated"], 1)
        self.assertEqual(data["summary"]["total"], 1)
        it = data["items"][0]
        for field in ("name", "kind", "status", "source", "agents", "locations", "installed", "latest", "modified", "notes", "changes"):
            self.assertIn(field, it)

    def test_output_never_contains_the_absolute_home_folder(self):
        self.stale_skill()
        for args in (["check", "-v"], ["check", "--json"], ["scan"], ["update", "--dry-run"]):
            rc, out, err = self.cli(*args)
            self.assertNotIn(str(self.home), out + err, args)
        self.assertIn("~/.agents/skills/demo", self.cli("check", "--json")[1])

    def test_exit_code_flag_is_for_ci(self):
        self.stale_skill()
        self.assertEqual(self.cli("check", "--exit-code")[0], 1)
        self.assertEqual(self.cli("check")[0], 0)

    def test_naming_a_source_selects_everything_from_it(self):
        self.stale_skill("one", source="acme/pack")
        names = [i["name"] for i in self.check("acme/pack")["items"]]
        self.assertEqual(names, ["one"])
        self.assertEqual(self.check("someone/else")["items"], [])

    def test_ignore_leaves_a_skill_out(self):
        self.stale_skill()
        data = self.check("--ignore", "demo")
        self.assertEqual(data["items"], [])
        self.assertTrue(any("--ignore" in n for n in data["notes"]))

    def test_config_file_can_ignore_skills_too(self):
        self.stale_skill()
        (self.tmp / "state").mkdir()
        (self.tmp / "state" / "config.json").write_text(json.dumps({"ignore": ["demo"]}))
        self.assertEqual(self.check()["items"], [])

    def test_second_update_has_nothing_left_to_do(self):
        self.stale_skill()
        self.assertEqual(self.cli("update", "--yes")[0], 0)
        rc, out, _ = self.cli("update", "--yes")
        self.assertEqual(rc, 0)
        self.assertIn("up to date", out)

    def test_unmanaged_skills_are_listed_but_never_touched(self):
        (self.claude / "handmade").mkdir(parents=True)
        (self.claude / "handmade" / "SKILL.md").write_text("---\nname: handmade\n---\n")
        data = self.check()
        self.assertEqual(self.item(data, "handmade")["status"], "unmanaged")
        self.assertEqual(self.cli("update", "--yes")[0], 0)
        self.assertTrue((self.claude / "handmade" / "SKILL.md").exists())

    def test_rollback_list_and_nothing_to_roll_back(self):
        rc, out, _ = self.cli("rollback", "--list")
        self.assertEqual((rc, out.strip()), (0, "No backups yet."))
        rc, _out, err = self.cli("rollback")
        self.assertEqual(rc, 2)
        self.assertIn("no backups", err)

    def test_no_backup_flag_discards_the_old_folder_after_verifying(self):
        self.stale_skill()
        rc, out, _ = self.cli("update", "--yes", "--no-backup", "--json")
        self.assertEqual(rc, 0)
        self.assertIn("v2", (self.agents / "demo" / "SKILL.md").read_text())
        backups = list((self.tmp / "state" / "backups").glob("*/dirs/*"))
        self.assertEqual(backups, [])

    @unittest.skipIf(os.name == "nt", "Windows cannot create a file name with control characters in it")
    def test_control_characters_in_names_never_reach_the_terminal(self):
        evil = self.claude / "evil\x1b[2J\x1b]0;pwned\x07name"
        evil.mkdir(parents=True)
        (evil / "SKILL.md").write_text("---\nname: x\n---\n")
        for args in (["check", "-v"], ["check", "--json"], ["update", "--dry-run"]):
            rc, out, err = self.cli(*args)
            self.assertNotIn("\x1b", out + err, args)
            self.assertNotIn("\x07", out + err, args)
        self.assertIn("evil[2J]0;pwnedname", self.cli("check", "-v")[1])

    def test_an_unexpected_error_is_a_short_message_not_a_traceback(self):
        import io
        from contextlib import redirect_stderr
        from unittest import mock
        from skillupdate import cli, lockfile
        err = io.StringIO()
        with mock.patch.object(lockfile, "check", side_effect=RuntimeError("boom")), redirect_stderr(err):
            self.assertEqual(cli.main(["check"]), 2)
        self.assertIn("unexpected error (RuntimeError): boom", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_script_runs_from_any_working_folder(self):
        import subprocess
        import sys
        r = subprocess.run([sys.executable, str(SCRIPTS / "skill_update.py"), "--version"], cwd=str(self.tmp),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn("skill-update", r.stdout)


if __name__ == "__main__":
    unittest.main()
