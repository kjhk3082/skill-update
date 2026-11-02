#!/usr/bin/env python3
"""End-to-end check against the real `npx skills` installer.

Installs this repository's skill with the real installer into a throw-away home folder and runs
skill-update on the result. That is the check that matters on Windows, where the installer leaves other
bytes than the upstream folder holds (CRLF line endings, no executable bits) and links folders with
junctions. It also exercises an update and a rollback on files the installer made.

    E2E_REPO=owner/repo E2E_REF=branch python3 tools/e2e_npx_skills.py

Needs Node (npx), git and network access. The real home folder is never touched.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "skill-update" / "scripts" / "skill_update.py"
REPO = os.environ.get("E2E_REPO") or "kjhk3082/skill-update"
REF = os.environ.get("E2E_REF") or "main"
OLD_REF = os.environ.get("E2E_OLD_REF") or "v0.1.0"   # an older version of the same skill, so there is something to update
SKILL = "skill-update"


def fail(message, *details):
    print("\nE2E FAILED: %s" % message)
    for d in details:
        print(d)
    sys.exit(1)


def step(text):
    print("\n== %s" % text, flush=True)


class Machine:
    """A home folder of its own, and the commands run inside it."""

    def __init__(self):
        self.home = Path(tempfile.mkdtemp(prefix="su-e2e-")).resolve()
        self.env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home), NO_COLOR="1")
        # the tool must cope with the console encoding it is given, so nothing here may force UTF-8 on it
        for name in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "SKILL_UPDATE_HOME", "SKILL_UPDATE_LOCK", "CLAUDE_CONFIG_DIR",
                     "PYTHONUTF8", "PYTHONIOENCODING"):
            self.env.pop(name, None)
        self.npx = shutil.which("npx")
        if not self.npx:
            fail("npx was not found; install Node.js")

    def run(self, cmd, timeout=300):
        return subprocess.run(cmd, env=self.env, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)

    @property
    def lock_path(self):
        return self.home / ".agents" / ".skill-lock.json"

    def install(self, ref):
        """`npx skills add owner/repo#ref -g`, retried: it needs the network."""
        source = "%s#%s" % (REPO, ref)
        for attempt in (1, 2, 3):
            r = self.run([self.npx, "--yes", "skills", "add", source, "-g", "-a", "claude-code", "-y"])
            if r.returncode == 0 and self.lock_path.is_file():
                return
            print("npx skills add %s failed (try %d): rc=%s\n%s\n%s" % (source, attempt, r.returncode, r.stdout[-1500:], r.stderr[-1500:]))
            time.sleep(5 * attempt)
        fail("npx skills add %s did not install the skill" % source)

    def tool(self, *args):
        r = self.run([sys.executable, str(SCRIPT), *args])
        if r.returncode not in (0, 1):
            fail("skill_update.py %s exited with %s" % (" ".join(args), r.returncode), r.stdout[-3000:], r.stderr[-3000:])
        return r

    def check(self):
        r = self.tool("check", "--json")
        try:
            data = json.loads(r.stdout)
        except ValueError:
            fail("check --json printed something that is not JSON", r.stdout[-3000:], r.stderr[-3000:])
        found = [i for i in data["items"] if i["name"] == SKILL]
        if not found:
            fail("check does not list %s" % SKILL, json.dumps(data, indent=2)[:4000])
        return found[0], data

    def describe(self):
        """What the installer left on disk, for the log: line endings and executable bits."""
        for base in (self.home / ".agents" / "skills" / SKILL, self.home / ".claude" / "skills" / SKILL):
            if not base.exists():
                continue
            kind = "junction/symlink" if (base.is_symlink() or getattr(os.path, "isjunction", lambda p: False)(base)) else "folder"
            print("  %s (%s)" % (base.relative_to(self.home).as_posix(), kind))
            if not base.is_symlink() and kind == "folder":
                crlf = sum(1 for p in base.rglob("*") if p.is_file() and b"\r\n" in p.read_bytes())
                execs = sum(1 for p in base.rglob("*") if p.is_file() and p.stat().st_mode & 0o111)
                print("    files with CRLF: %d, with an executable bit: %d" % (crlf, execs))


def main():
    if hasattr(sys.stdout, "reconfigure"):  # the installer's output has box-drawing characters
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("source: %s#%s   older version: %s#%s   python: %s" % (REPO, REF, REPO, OLD_REF, sys.version.split()[0]))
    m = Machine()
    try:
        step("1. install the skill from %s with the real installer; skill-update must call it current" % REF)
        m.install(REF)
        m.describe()
        item, data = m.check()
        print("  status=%s modified=%s notes=%s" % (item["status"], item["modified"], item["notes"]))
        if item["status"] != "current" or item["modified"] or any("local edits" in n for n in item["notes"]):
            fail("a fresh install is not reported as current and untouched", json.dumps(item, indent=2))

        step("2. install the older %s, point the lock at %s, and update" % (OLD_REF, REF))
        m.install(OLD_REF)
        lock = json.loads(m.lock_path.read_text(encoding="utf-8"))
        entry = lock["skills"][SKILL]
        entry["ref"] = REF   # the same skill now follows the branch under test, which is ahead of the older version
        m.lock_path.write_text(json.dumps(lock, indent=2), encoding="utf-8")
        item, _ = m.check()
        print("  status=%s modified=%s changes=%s" % (item["status"], item["modified"], item.get("changes", {}).get("modified")))
        if item["status"] != "outdated":
            fail("the older version is not reported as outdated", json.dumps(item, indent=2))
        r = m.tool("update", "--yes", "--json")
        result = json.loads(r.stdout)
        print("  update: %s" % [(x["name"], x["outcome"], x["detail"]) for x in result["results"]])
        if [x["outcome"] for x in result["results"] if x["name"] == SKILL] != ["updated"]:
            fail("the update did not succeed", r.stdout[-3000:], r.stderr[-3000:])
        m.describe()
        item, _ = m.check()
        if item["status"] != "current":
            fail("after the update the skill is not current", json.dumps(item, indent=2))
        tree_after = json.loads(m.lock_path.read_text(encoding="utf-8"))["skills"][SKILL]["skillFolderHash"]

        step("3. roll the update back")
        r = m.tool("rollback", result["run_id"])
        print("  " + r.stdout.strip().replace("\n", "\n  "))
        item, _ = m.check()
        if item["status"] != "outdated":
            fail("after the rollback the older version is not reported as outdated", json.dumps(item, indent=2))
        if json.loads(m.lock_path.read_text(encoding="utf-8"))["skills"][SKILL]["skillFolderHash"] == tree_after:
            fail("the rollback left the updated lock record in place")
        print("\nE2E OK")
    finally:
        shutil.rmtree(m.home, ignore_errors=True)


if __name__ == "__main__":
    main()
