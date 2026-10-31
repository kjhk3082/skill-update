"""Find every installed skill folder and group the entries that are really the same folder."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .util import home, short

SKILL_FILE = "SKILL.md"

# (agent, folder): the global skill folders documented by the `skills` CLI, plus legacy ones that
# are still common. Folders that do not exist are skipped. Add your own with --root.
KNOWN_ROOTS: List[Tuple[str, str]] = [
    ("adal", "~/.adal/skills"),
    ("universal", "~/.agents/skills"),  # Codex, Cline, Pi, Warp, Zed and others share this one
    ("aider-desk", "~/.aider-desk/skills"),
    ("astrbot", "~/.astrbot/data/skills"),
    ("augment", "~/.augment/skills"),
    ("autohand", "~/.autohand/skills"),
    ("bob", "~/.bob/skills"),
    ("claude-code", "~/.claude/skills"),
    ("codearts", "~/.codeartsdoer/skills"),
    ("codebuddy", "~/.codebuddy/skills"),
    ("windsurf", "~/.codeium/windsurf/skills"),
    ("windsurf", "~/.windsurf/skills"),
    ("codemaker", "~/.codemaker/skills"),
    ("codestudio", "~/.codestudio/skills"),
    ("codex", "~/.codex/skills"),
    ("command-code", "~/.commandcode/skills"),
    ("amp", "~/.config/agents/skills"),
    ("crush", "~/.config/crush/skills"),
    ("devin", "~/.config/devin/skills"),
    ("goose", "~/.config/goose/skills"),
    ("kimchi", "~/.config/kimchi/harness/skills"),
    ("opencode", "~/.config/opencode/skills"),
    ("continue", "~/.continue/skills"),
    ("github-copilot", "~/.copilot/skills"),
    ("cursor", "~/.cursor/skills"),
    ("deep-agents", "~/.deepagents/agent/skills"),
    ("droid", "~/.factory/skills"),
    ("firebender", "~/.firebender/skills"),
    ("forge", "~/.forge/skills"),
    ("antigravity-cli", "~/.gemini/antigravity-cli/skills"),
    ("antigravity", "~/.gemini/antigravity/skills"),
    ("gemini-cli", "~/.gemini/skills"),
    ("grok", "~/.grok/skills"),
    ("hermes-agent", "~/.hermes/skills"),
    ("iflow", "~/.iflow/skills"),
    ("inference-sh", "~/.inferencesh/skills"),
    ("jazz", "~/.jazz/skills"),
    ("junie", "~/.junie/skills"),
    ("kilo", "~/.kilo/skills"),
    ("kiro-cli", "~/.kiro/skills"),
    ("kode", "~/.kode/skills"),
    ("lingma", "~/.lingma/skills"),
    ("mcpjam", "~/.mcpjam/skills"),
    ("minimax", "~/.minimax/skills"),
    ("moxby", "~/.moxby/skills"),
    ("mux", "~/.mux/skills"),
    ("neovate", "~/.neovate/skills"),
    ("ona", "~/.ona/skills"),
    ("openclaw", "~/.openclaw/skills"),
    ("openhands", "~/.openhands/skills"),
    ("pochi", "~/.pochi/skills"),
    ("posit", "~/.posit/assistant/skills"),
    ("qoder-cn", "~/.qoder-cn/skills"),
    ("qoder", "~/.qoder/skills"),
    ("qwen-code", "~/.qwen/skills"),
    ("reasonix", "~/.reasonix/skills"),
    ("roo", "~/.roo/skills"),
    ("rovodev", "~/.rovodev/skills"),
    ("cortex", "~/.snowflake/cortex/skills"),
    ("tabnine", "~/.tabnine/agent/skills"),
    ("terramind", "~/.terramind/skills"),
    ("tinycloud", "~/.tinycloud/skills"),
    ("trae-cn", "~/.trae-cn/skills"),
    ("trae", "~/.trae/skills"),
    ("mistral-vibe", "~/.vibe/skills"),
    ("zcode", "~/.zcode/skills"),
    ("zencoder", "~/.zencoder/skills"),
]

# Agents that let the user move their config folder with an environment variable.
_ENV_ROOTS = {"claude-code": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME", "hermes-agent": "HERMES_HOME"}


@dataclass
class Exposure:
    """One place an agent can see the skill (a real folder or a symlink to it)."""
    agent: str
    path: Path


@dataclass
class Skill:
    name: str
    real: Path                      # where the files actually live
    exposures: List[Exposure] = field(default_factory=list)
    in_root: bool = False           # the real folder sits directly inside a skill folder we scan

    @property
    def agents(self) -> List[str]:
        return sorted({e.agent for e in self.exposures})


@dataclass
class Scan:
    skills: List[Skill]
    roots: List[Tuple[str, Path, int]]  # (agent, folder, number of skills found)
    containers: int                      # sub-folders without a SKILL.md (category folders, e.g. Hermes)


def known_roots() -> List[Tuple[str, Path]]:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    out: List[Tuple[str, Path]] = []
    for agent, template in KNOWN_ROOTS:
        env = os.environ.get(_ENV_ROOTS.get(agent, ""), "")
        if env:
            path = Path(env).expanduser() / "skills"
        elif xdg and template.startswith("~/.config/"):
            path = Path(xdg).expanduser() / template[len("~/.config/"):]
        else:
            path = home() / template[2:]
        out.append((agent, path))
    return out


def scan(extra_roots: Iterable[str] = ()) -> Scan:
    roots: Dict[str, Tuple[str, Path]] = {}  # real path -> (agent, path); aliases of one folder count once
    candidates = known_roots() + [("custom", Path(r).expanduser()) for r in extra_roots]
    for agent, path in candidates:
        if path.is_dir():
            roots.setdefault(os.path.realpath(path), (agent, path))

    found: Dict[str, Skill] = {}
    counts: Dict[str, int] = {}
    containers = 0
    for real_root, (agent, path) in roots.items():
        counts[real_root] = 0
        try:
            entries = sorted(os.scandir(path), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith(".") or not os.path.isdir(entry.path):
                continue
            if not os.path.isfile(os.path.join(entry.path, SKILL_FILE)):
                containers += 1
                continue
            real = os.path.realpath(entry.path)
            skill = found.get(real)
            if skill is None:
                skill = found[real] = Skill(name=entry.name, real=Path(real),
                                            in_root=os.path.dirname(real) in roots)
            skill.exposures.append(Exposure(agent, Path(entry.path)))
            counts[real_root] += 1

    for skill in found.values():
        names = [e.path.name for e in skill.exposures]
        skill.name = skill.real.name if skill.real.name in names else names[0]
    skills = sorted(found.values(), key=lambda s: (s.name.lower(), str(s.real)))
    summary = [(agent, path, counts[real]) for real, (agent, path) in roots.items()]
    return Scan(skills=skills, roots=summary, containers=containers)


def explain_unmanaged(skill: Skill) -> List[str]:
    """Why a skill without a known upstream was left alone, when there is something worth saying."""
    if not skill.in_root:
        return ["symlink to %s, outside your skill folders; not touched" % short(skill.real)]
    for parent in skill.real.parents:
        if (parent / ".git").exists():
            return ["lives inside the git repository %s; not touched" % short(parent)]
        if parent == home():
            break
    return []
