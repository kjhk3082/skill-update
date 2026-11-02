"""The update policy in isolation: which outdated items a run touches, and why it leaves some out."""
import unittest
from types import SimpleNamespace

from helpers import ROOT  # noqa: F401  (puts the package on sys.path)

from skillupdate import policy
from skillupdate.model import KIND_GIT, OUTDATED, Item


def opts(*names, keep_modified=False):
    return SimpleNamespace(names=list(names), keep_modified=keep_modified)


def clone(**kw):
    return Item(name="pack", kind=KIND_GIT, status=OUTDATED, source="owner/pack", **kw)


class SelectTests(unittest.TestCase):
    def test_an_outdated_item_is_chosen(self):
        chosen, skipped = policy.select([clone()], opts())
        self.assertEqual([i.name for i in chosen], ["pack"])
        self.assertEqual(skipped, [])

    def test_a_setup_script_keeps_a_clone_out_unless_it_is_named(self):
        item = clone(setup_script=True)
        chosen, skipped = policy.select([item], opts())
        self.assertEqual(chosen, [])
        self.assertIn("setup script", skipped[0][1])
        self.assertIn("update pack", skipped[0][1])           # the reason says how to get it updated anyway
        for named in ("pack", "owner/pack"):                  # by name, or by its source
            chosen, skipped = policy.select([item], opts(named))
            self.assertEqual([i.name for i in chosen], ["pack"], named)
            self.assertEqual(skipped, [], named)

    def test_naming_something_else_selects_nothing_and_reports_nothing(self):
        self.assertEqual(policy.select([clone(setup_script=True)], opts("other")), ([], []))

    def test_the_setup_script_is_the_one_reason_given_even_when_keep_modified_applies_too(self):
        chosen, skipped = policy.select([clone(setup_script=True, modified=True)], opts(keep_modified=True))
        self.assertEqual(chosen, [])
        self.assertEqual(len(skipped), 1)
        self.assertIn("setup script", skipped[0][1])


if __name__ == "__main__":
    unittest.main()
