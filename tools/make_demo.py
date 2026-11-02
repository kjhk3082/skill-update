#!/usr/bin/env python3
"""Regenerate assets/demo-check.svg and assets/demo-update.svg.

Builds a throw-away machine full of made-up skills, runs the real skill-update against it and
renders the real terminal output as SVG, so the README never shows output the tool cannot produce.

    python3 tools/make_demo.py
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "skill-update" / "scripts" / "skill_update.py"
sys.path.insert(0, str(ROOT / "tests"))
from helpers import GIT_ENV, Upstream, git  # noqa: E402

FAKE_CLAUDE = """#!%s
import json, os, sys
a = sys.argv[1:]
if a[:3] == ["plugin", "marketplace", "update"]:
    sys.exit(0)
if a[:2] == ["plugin", "update"]:
    p = os.path.join(os.environ["HOME"], ".claude", "plugins", "installed_plugins.json")
    d = json.load(open(p))
    if a[2] == "lint-fixer@acme-plugins":
        d["plugins"][a[2]][0]["version"] = "1.3.0"
        json.dump(d, open(p, "w"))
    sys.exit(0)
sys.exit(2)
""" % sys.executable


def skill(name, body):
    return "---\nname: %s\ndescription: Use when %s\n---\n%s\n" % (name, name.replace("-", " "), body)


def build(tmp: Path):
    home, state = tmp / "home", tmp / "state"
    (home / ".claude" / "skills").mkdir(parents=True)
    (home / ".agents" / "skills").mkdir(parents=True)
    agents, claude = home / ".agents" / "skills", home / ".claude" / "skills"

    design = Upstream(tmp, "design-skills")
    office = Upstream(tmp, "office-skills")
    old = Upstream(tmp, "old-skills")
    # The packs sit at <tmp>/github.com/acme/<name>.git, so the address git resolves them to reads like the
    # GitHub repository they stand for: the source label is taken from a github.com/owner/repo path.
    packs = {n: Upstream(tmp / "github.com" / "acme", n) for n in ("review-pack", "team-pack")}
    design.commit({"skills/%s/SKILL.md" % n: skill(n, "v1") for n in ("frontend-design", "motion-guide", "color-theory")})
    office.commit({"skills/pdf-tools/SKILL.md": skill("pdf-tools", "v1"), "skills/pdf-tools/scripts/convert.py": "print('v1')\n",
                   "skills/docx-tools/SKILL.md": skill("docx-tools", "v1"), "skills/sql-helper/SKILL.md": skill("sql-helper", "v1")})
    old.commit({"skills/old-style/SKILL.md": skill("old-style", "v1")})
    for name, up in packs.items():
        up.commit({"SKILL.md": skill(name, "v1"), "setup": ("#!/bin/sh\n", 0o755) if name == "review-pack" else "x\n"})

    gitcfg = tmp / "gitconfig"  # make https://github.com/acme/... resolve to the local repositories
    gitcfg.write_text("".join('[url "file://%s"]\n\tinsteadOf = https://github.com/acme/%s.git\n' % (u.bare, n)
                              for n, u in [("design-skills", design), ("office-skills", office), ("old-skills", old), *packs.items()]))
    env = dict(os.environ)
    env.update(GIT_ENV)
    env.update(HOME=str(home), SKILL_UPDATE_HOME=str(state), GIT_CONFIG_GLOBAL=str(gitcfg),
               FORCE_COLOR="1", SKILL_UPDATE_CLAUDE=str(tmp / "claude"))
    env.pop("NO_COLOR", None)

    lock = {}
    def install(up, repo, name):
        folder = "skills/" + name
        shutil.copytree(up.work / folder, agents / name, ignore=shutil.ignore_patterns(".git"))
        lock[name] = {"source": "acme/" + repo, "sourceType": "github", "sourceUrl": "https://github.com/acme/%s.git" % repo,
                      "skillPath": folder + "/SKILL.md", "skillFolderHash": up.tree(folder),
                      "installedAt": "2026-08-01T09:00:00.000Z", "updatedAt": "2026-08-01T09:00:00.000Z"}
    for n in ("frontend-design", "motion-guide", "color-theory"):
        install(design, "design-skills", n)
    for n in ("pdf-tools", "docx-tools", "sql-helper"):
        install(office, "office-skills", n)
    install(old, "old-skills", "old-style")
    (agents / "pdf-tools" / "scripts" / "convert.py").write_text("print('my own tweak')\n")  # a local edit
    (home / ".agents" / ".skill-lock.json").write_text(json.dumps({"version": 3, "skills": lock, "dismissed": {}, "lastSelectedAgents": []}, indent=2))

    for n, up in packs.items():
        subprocess.run(["git", "clone", "-q", "https://github.com/acme/%s.git" % n, str(claude / n)], env=env, check=True)
    (claude / "team-pack" / "mine.md").write_text("local commit\n")
    git("add", "-A", cwd=claude / "team-pack")
    git("commit", "-q", "-m", "local", cwd=claude / "team-pack")
    (claude / "my-notes").mkdir()
    (claude / "my-notes" / "SKILL.md").write_text(skill("my-notes", "hand written"))

    # time passes: upstream moves on
    design.commit({"skills/frontend-design/SKILL.md": skill("frontend-design", "v2"), "skills/motion-guide/SKILL.md": skill("motion-guide", "v2"),
                   "skills/color-theory/SKILL.md": skill("color-theory", "v2"), "skills/color-theory/palettes.md": "new\n"})
    office.commit({"skills/pdf-tools/SKILL.md": skill("pdf-tools", "v2"), "skills/pdf-tools/scripts/convert.py": "print('v2')\n"})
    old.commit({"skills/old-style": None, "skills/something-else/SKILL.md": skill("something-else", "v1")})
    packs["review-pack"].commit({"SKILL.md": skill("review-pack", "v2"), "references/new.md": "x\n"}, "add references")
    packs["team-pack"].commit({"SKILL.md": skill("team-pack", "v2")}, "rewrite")

    plugins = home / ".claude" / "plugins"
    market = plugins / "marketplaces" / "acme-plugins"
    (market / ".claude-plugin").mkdir(parents=True)
    (market / ".claude-plugin" / "marketplace.json").write_text(json.dumps({"name": "acme-plugins", "plugins": [
        {"name": "lint-fixer", "source": "./lint-fixer", "version": "1.3.0"}, {"name": "deploy-helper", "source": "./d", "version": "2.0.1"},
        {"name": "chat-tools", "source": "./c", "version": "0.9.0"}, {"name": "mystery", "source": {"source": "github", "repo": "acme/mystery"}}]}))
    (plugins / "known_marketplaces.json").write_text(json.dumps({"acme-plugins": {"installLocation": str(market), "lastUpdated": "2026-10-30T00:00:00Z"}}))
    entry = lambda v: [{"scope": "user", "version": v, "installPath": str(plugins / "cache" / v)}]
    (plugins / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {
        "lint-fixer@acme-plugins": entry("1.2.0"), "deploy-helper@acme-plugins": entry("2.0.1"),
        "chat-tools@acme-plugins": entry("0.9.0"), "mystery@acme-plugins": entry("0.3.0")}}))
    fake = tmp / "claude"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return env


def run(env, *args):
    r = subprocess.run([sys.executable, str(SCRIPT), *args], env=env, capture_output=True, text=True, check=True)
    return r.stdout


# -- rendering -------------------------------------------------------------------------------

PALETTE = {"0": ("#c9d1d9", 400), "1": ("#ffffff", 700), "2": ("#6e7681", 400), "31": ("#ff7b72", 400),
           "32": ("#7ee787", 400), "33": ("#e3b341", 400), "36": ("#79c0ff", 400)}
SGR = re.compile(r"\033\[([0-9;]*)m")
CHAR_W, LINE_H, PAD_X, PAD_TOP = 7.85, 19, 24, 52


def segments(line):
    pos, code, out = 0, "0", []
    for m in SGR.finditer(line):
        if m.start() > pos:
            out.append((code, line[pos:m.start()]))
        code = m.group(1) or "0"
        pos = m.end()
    if pos < len(line):
        out.append((code, line[pos:]))
    return out


def render_svg(text, label):
    lines = text.rstrip("\n").split("\n")
    visible = [SGR.sub("", ln) for ln in lines]
    width = int(max(len(v) for v in visible) * CHAR_W + PAD_X * 2)
    height = PAD_TOP + len(lines) * LINE_H + 22
    rows = []
    for i, ln in enumerate(lines):
        y = PAD_TOP + i * LINE_H
        spans = "".join('<tspan fill="%s" font-weight="%d">%s</tspan>' % (*PALETTE.get(code, PALETTE["0"]), escape(seg))
                        for code, seg in segments(ln))
        rows.append('<text x="%d" y="%d" xml:space="preserve">%s</text>' % (PAD_X, y, spans))
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" role="img" aria-label="%s">\n'
            '<rect width="%d" height="%d" rx="10" fill="#0d1117"/>\n'
            '<circle cx="22" cy="20" r="6" fill="#ff5f56"/><circle cx="42" cy="20" r="6" fill="#ffbd2e"/><circle cx="62" cy="20" r="6" fill="#27c93f"/>\n'
            '<g font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, \'DejaVu Sans Mono\', monospace" font-size="13">\n%s\n</g>\n</svg>\n'
            % (width, height, width, height, escape(label), width, height, "\n".join(rows)))


def main():
    tmp = Path(tempfile.mkdtemp(prefix="su-demo-")).resolve()
    try:
        env = build(tmp)
        check = run(env, "check")
        update = run(env, "update", "--yes")
    finally:
        pass
    out = ROOT / "assets"
    out.mkdir(exist_ok=True)
    (out / "demo-check.svg").write_text(render_svg("$ python3 skill_update.py check\n" + check.replace(str(tmp), "~"), "skill-update check output"))
    (out / "demo-update.svg").write_text(render_svg("$ python3 skill_update.py update --yes\n" + update.replace(str(tmp), "~"), "skill-update update output"))
    print(check)
    print(update)
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
