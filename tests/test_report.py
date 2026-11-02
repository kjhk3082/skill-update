"""What a person reads: the update plan and its skip list."""
import unittest

from helpers import ROOT  # noqa: F401  (puts the package on sys.path)

from skillupdate import report
from skillupdate.model import KIND_GIT, OUTDATED, Item


def clone(folder):
    return Item(name="pack", kind=KIND_GIT, status=OUTDATED, locations=[folder])


class PlanTests(unittest.TestCase):
    plain = report.make_style(True)

    def test_a_skipped_item_is_listed_with_its_reason(self):
        text = report.render_plan([], [(clone("~/.claude/skills/pack"), "has a setup script")], self.plain)
        self.assertIn("Skipping:", text)
        self.assertIn("   pack — has a setup script", text)

    def test_skipped_copies_of_one_skill_are_told_apart_by_their_folder(self):
        a, b = clone("~/.claude/skills/pack"), clone("~/.agents/skills/pack")
        text = report.render_plan([], [(a, "why"), (b, "why")], self.plain)
        self.assertIn("pack [~/.claude/skills/pack] — why", text)
        self.assertIn("pack [~/.agents/skills/pack] — why", text)


if __name__ == "__main__":
    unittest.main()
