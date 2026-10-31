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
        # plugins: one with a newer catalog version, one current, one whose catalog publishes no version,
        # one the marketplace dropped, one it renamed
        catalog = {"name": "acme", "renames": {"moved": "moved-v2"}, "plugins": [
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
            "gone@acme": entry("0.1.0"), "moved@acme": entry("0.2.0"),
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
                         {"fresh@acme": "outdated", "same@acme": "current", "mystery@acme": "unknown",
                          "gone@acme": "blocked", "moved@acme": "blocked"})
        self.assertEqual(self.item(data, "fresh@acme")["latest"], "2.0.0")
        self.assertFalse(self.log.exists(), "check must not run claude unless --refresh is given")

    def test_plugins_the_marketplace_no_longer_lists_are_reported_not_attempted(self):
        data = self.check("--only", "plugin")
        self.assertIn("no longer listed", self.item(data, "gone@acme")["notes"][0])
        self.assertIn("renamed to moved-v2", self.item(data, "moved@acme")["notes"][0])
        rc, out, err = self.cli("update", "--yes", "--only", "plugin", "--json")
        self.assertEqual(rc, 0, err)                                    # nothing failed: they were never attempted
        names = {r["name"] for r in json.loads(out)["results"]}
        self.assertFalse({"gone@acme", "moved@acme"} & names)
        self.assertFalse([c for c in self.log.read_text().splitlines() if "gone@acme" in c or "moved@acme" in c])

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

    def test_a_plugin_id_with_control_characters_is_never_passed_on_even_when_it_displays_cleanly(self):
        path = self.plugins_dir / "installed_plugins.json"
        data = json.loads(path.read_text())
        data["plugins"]["fresh\x1b@acme"] = [{"scope": "user", "version": "1", "installPath": "/x"}]
        path.write_text(json.dumps(data))
        catalog_path = self.market / ".claude-plugin" / "marketplace.json"
        catalog = json.loads(catalog_path.read_text())
        catalog["plugins"].append({"name": "fresh\x1b", "source": {"source": "github", "repo": "evil/x"}})  # listed, no version
        catalog_path.write_text(json.dumps(catalog))
        rc, out, _ = self.cli("update", "--yes", "--only", "plugin", "--json")
        # both entries display as `fresh@acme`; the genuine one is handled, the look-alike is refused
        outcomes = sorted(r["outcome"] for r in json.loads(out)["results"] if r["name"] == "fresh@acme")
        self.assertEqual(outcomes, ["failed", "unchanged"])
        self.assertNotIn("\x1b", out)
        self.assertEqual([c for c in self.log.read_text().splitlines() if c.startswith("plugin update fresh@acme")],
                         ["plugin update fresh@acme --scope user"])   # only the genuine plugin, once

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
