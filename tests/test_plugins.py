import json
import os
import stat
import sys
import unittest
from unittest import mock

from helpers import Sandbox

FAKE_CLAUDE = """#!%s
import json, os, sys
args = sys.argv[1:]
open(os.environ["FAKE_CLAUDE_LOG"], "a").write(" ".join(args) + "\\n")
if args[:3] == ["plugin", "marketplace", "update"]:
    sys.exit(int(os.environ.get("FAKE_CLAUDE_REFRESH_RC", "0")))
if args[:2] == ["plugin", "update"]:
    key = args[2]
    if key in os.environ.get("FAKE_CLAUDE_FAIL", "").split(","):
        sys.stderr.write("error: network unreachable\\n")
        sys.exit(1)
    path = os.path.join(os.environ["HOME"], ".claude", "plugins", "installed_plugins.json")
    data = json.load(open(path))
    new = json.loads(os.environ.get("FAKE_CLAUDE_VERSIONS", "{}")).get(key)
    if new:
        data["plugins"][key][0]["version"] = new
        json.dump(data, open(path, "w"))
    print("updated" if new else "already up to date")
    sys.exit(0)
sys.exit(2)
""" % sys.executable


class PluginChannelTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.plugins_dir = self.home / ".claude" / "plugins"
        self.market = self.plugins_dir / "marketplaces" / "acme"
        (self.market / ".claude-plugin").mkdir(parents=True)
        self.plugins_dir.mkdir(exist_ok=True)
        # three plugins: one with a newer catalog version, one current, one whose catalog publishes no version
        catalog = {"name": "acme", "plugins": [
            {"name": "fresh", "source": "./plugins/fresh", "version": "2.0.0"},
            {"name": "same", "source": "./plugins/same"},
            {"name": "mystery", "source": {"source": "github", "repo": "acme/mystery"}},
        ]}
        (self.market / ".claude-plugin" / "marketplace.json").write_text(json.dumps(catalog))
        (self.market / "plugins" / "same" / ".claude-plugin").mkdir(parents=True)
        (self.market / "plugins" / "same" / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "same", "version": "1.1.0"}))
        (self.plugins_dir / "known_marketplaces.json").write_text(json.dumps(
            {"acme": {"source": {"source": "github", "repo": "acme/market"}, "installLocation": str(self.market),
                      "lastUpdated": "2026-10-30T00:00:00.000Z"}}))
        entry = lambda v: [{"scope": "user", "version": v, "installPath": str(self.plugins_dir / "cache" / "x"), "gitCommitSha": "abc"}]
        (self.plugins_dir / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {
            "fresh@acme": entry("1.0.0"), "same@acme": entry("1.1.0"), "mystery@acme": entry("0.3.0"),
            "scoped@acme": [{"scope": "project", "version": "9", "installPath": "/x"}]}}))

        self.log = self.tmp / "claude.log"
        fake = self.tmp / "claude"
        fake.write_text(FAKE_CLAUDE)
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        env = mock.patch.dict(os.environ, {"SKILL_UPDATE_CLAUDE": str(fake), "FAKE_CLAUDE_LOG": str(self.log)})
        env.start()
        self.addCleanup(env.stop)

    def test_versions_decide_the_status_and_unknown_is_not_guessed(self):
        data = self.check("--only", "plugin")
        self.assertEqual({i["name"]: i["status"] for i in data["items"]},
                         {"fresh@acme": "outdated", "same@acme": "current", "mystery@acme": "unknown"})
        self.assertEqual(self.item(data, "fresh@acme")["latest"], "2.0.0")
        self.assertFalse(self.log.exists(), "check must not run claude unless --refresh is given")

    def test_plugins_installed_for_one_project_only_are_skipped(self):
        names = [i["name"] for i in self.check("--only", "plugin")["items"]]
        self.assertNotIn("scoped@acme", names)

    def test_update_refreshes_catalogs_then_asks_claude_to_update_outdated_and_unknown_plugins(self):
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_VERSIONS": json.dumps({"fresh@acme": "2.0.0"})}):
            rc, out, err = self.cli("update", "--yes", "--only", "plugin", "--json")
        self.assertEqual(rc, 0, err)
        results = {r["name"]: r for r in json.loads(out)["results"]}
        self.assertEqual(results["fresh@acme"]["outcome"], "updated")
        self.assertEqual(results["fresh@acme"]["detail"], "1.0.0 → 2.0.0")
        self.assertEqual(results["mystery@acme"]["outcome"], "unchanged")
        self.assertNotIn("same@acme", results)
        calls = self.log.read_text().splitlines()
        self.assertEqual(calls[0], "plugin marketplace update")
        self.assertIn("plugin update fresh@acme --scope user", calls)
        self.assertTrue(any("Restart Claude Code" in n for n in json.loads(out)["notes"]))

    def test_dry_run_never_calls_claude(self):
        rc, out, _ = self.cli("update", "--dry-run", "--only", "plugin")
        self.assertEqual(rc, 0)
        self.assertIn("Dry run", out)
        self.assertFalse(self.log.exists())

    def test_catalogs_are_refreshed_once_per_update(self):
        self.cli("update", "--yes", "--only", "plugin")
        self.assertEqual(self.log.read_text().splitlines().count("plugin marketplace update"), 1)

    def test_a_failing_plugin_does_not_stop_the_rest(self):
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_FAIL": "fresh@acme", "FAKE_CLAUDE_VERSIONS": json.dumps({"mystery@acme": "0.4.0"})}):
            rc, out, _ = self.cli("update", "--yes", "--only", "plugin", "--json")
        self.assertEqual(rc, 1)
        results = {r["name"]: r["outcome"] for r in json.loads(out)["results"]}
        self.assertEqual(results, {"fresh@acme": "failed", "mystery@acme": "updated"})

    def test_missing_claude_command_is_reported_per_plugin(self):
        with mock.patch.dict(os.environ, {"SKILL_UPDATE_CLAUDE": str(self.tmp / "nope")}):
            rc, out, _ = self.cli("update", "--yes", "--only", "plugin", "--json")
        self.assertEqual(rc, 1)
        self.assertTrue(all(r["outcome"] == "failed" for r in json.loads(out)["results"]))

    def test_no_plugins_installed_means_no_plugin_items(self):
        (self.plugins_dir / "installed_plugins.json").unlink()
        self.assertEqual(self.check("--only", "plugin")["items"], [])


if __name__ == "__main__":
    unittest.main()
