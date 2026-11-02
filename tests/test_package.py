"""The metadata people see (SKILL.md, plugin manifests, README links) must agree with the code."""
import json
import re
import unittest

from helpers import ROOT, SCRIPTS  # noqa: F401

from skillupdate import __version__, discover

SKILL = ROOT / "skills" / "skill-update" / "SKILL.md"


class PackageTests(unittest.TestCase):
    def frontmatter(self):
        text = SKILL.read_text(encoding="utf-8")
        block = text.split("---")[1]
        return dict(re.findall(r"^(\w+):\s*(.+)$", block, re.M))

    def test_skill_md_follows_the_skill_format(self):
        fm = self.frontmatter()
        self.assertEqual(fm["name"], "skill-update")
        self.assertEqual(SKILL.parent.name, fm["name"])
        self.assertTrue(fm["description"].startswith("Use when"))
        self.assertLessEqual(len(fm["description"]), 1024)

    def test_versions_agree(self):
        plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
        market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
        self.assertEqual(plugin["version"], __version__)
        self.assertEqual(market["plugins"][0]["version"], __version__)
        self.assertEqual(market["metadata"]["version"], __version__)
        self.assertEqual(plugin["name"], market["plugins"][0]["name"])
        self.assertIn("## " + __version__, (ROOT / "CHANGELOG.md").read_text())

    def test_readme_images_and_links_exist(self):
        for readme in ("README.md", "README.ko.md"):
            text = (ROOT / readme).read_text(encoding="utf-8")
            for target in re.findall(r'(?:src="|\]\()((?!https?:)[^")#]+)', text):
                self.assertTrue((ROOT / target).exists(), "%s links to missing %s" % (readme, target))

    def test_check_screenshot_shows_the_current_version(self):
        svg = (ROOT / "assets" / "demo-check.svg").read_text(encoding="utf-8")
        self.assertIn(">skill-update %s<" % __version__, svg,
                      "assets/demo-check.svg shows another version: run python3 tools/make_demo.py")

    def test_every_known_folder_is_a_skills_folder_under_home(self):
        for agent, template in discover.KNOWN_ROOTS:
            self.assertTrue(template.startswith("~/."), template)
            self.assertTrue(template.endswith("skills"), template)

    def test_changelog_counts_match_the_code(self):
        changelog = (ROOT / "CHANGELOG.md").read_text()
        agents = {a for a, _ in discover.KNOWN_ROOTS}
        self.assertIn("%d known skill folders of %d agents" % (len(discover.KNOWN_ROOTS), len(agents)), changelog)


if __name__ == "__main__":
    unittest.main()
