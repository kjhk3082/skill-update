import os
import unittest

from helpers import Sandbox

from skillupdate import discover


class DiscoverTests(Sandbox):
    def make(self, root, name):
        d = root / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: %s\n---\n" % name)
        return d

    def test_finds_skills_in_known_folders_and_merges_symlinks(self):
        real = self.make(self.agents, "alpha")
        self.claude.mkdir(parents=True)
        os.symlink(real, self.claude / "alpha")
        self.make(self.claude, "beta")
        sc = discover.scan()
        names = {s.name: s for s in sc.skills}
        self.assertEqual(sorted(names), ["alpha", "beta"])
        self.assertEqual(names["alpha"].agents, ["claude-code", "universal"])  # one skill, two agents
        self.assertTrue(names["alpha"].in_root and names["beta"].in_root)

    def test_ignores_hidden_folders_and_counts_category_folders(self):
        self.make(self.agents, ".system")
        (self.agents / "category" / "inner").mkdir(parents=True)
        self.make(self.agents, "real")
        sc = discover.scan()
        self.assertEqual([s.name for s in sc.skills], ["real"])
        self.assertEqual(sc.containers, 1)

    def test_symlink_to_outside_is_not_in_root(self):
        outside = self.make(self.tmp / "elsewhere", "mine")
        self.claude.mkdir(parents=True)
        os.symlink(outside, self.claude / "mine")
        (skill,) = discover.scan().skills
        self.assertFalse(skill.in_root)
        self.assertIn("outside your skill folders", discover.explain_unmanaged(skill)[0])

    def test_extra_root(self):
        self.make(self.tmp / "custom", "x")
        self.assertEqual([s.name for s in discover.scan([str(self.tmp / "custom")]).skills], ["x"])


if __name__ == "__main__":
    unittest.main()
