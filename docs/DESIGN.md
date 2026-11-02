# Design

How skill-update decides what is outdated, and how it changes things without losing yours.

## Goal

One command that answers "which of the skills on this machine have a newer version?" and then updates them, for skills installed any of the usual ways, in any agent's skill folder.

Not goals: installing new skills (use `npx skills add`), judging whether an update is a good idea, or managing MCP servers.

## How skills get onto a machine

| Channel | Where the facts are | What can be known about upstream |
|---|---|---|
| `npx skills add` | `~/.agents/.skill-lock.json`: source repository, path in it, `skillFolderHash` | the newest folder in the repository |
| `git clone` into a skills folder | the clone's own `.git` | its tracking branch |
| Claude Code plugin | `~/.claude/plugins/installed_plugins.json` plus marketplace catalogs | a published version, sometimes |
| copied by hand | nothing | nothing |

skill-update has one module per channel (`lockfile.py`, `gitskill.py`, `plugins.py`). Everything else (scanning, hashing, backup, reporting) is shared.

## Pipeline

```
scan folders ─▶ claim skills per channel ─▶ compare with upstream ─▶ plan ─▶ confirm ─▶ apply ─▶ verify ─▶ report
  discover.py        lock → git → plugin        treehash.py / gitops.py   policy.py             backup.py
```

`check` stops after the comparison and never writes outside its own cache (`~/.skill-update/cache`). `update` runs the whole pipeline.

## Comparing folders by hash

The lock file stores, for every skill, the git **tree hash** of the skill's folder at install time. `treehash.py` computes that hash for any folder on disk exactly as git does (blob hashes, modes `100644`/`100755`/`120000`, git's sorting rule where a directory sorts as if its name ended in `/`). A test compares the result with `git write-tree` on a tree that exercises the awkward cases (a file `a-b` between a folder `a` and a file `a.txt`, an executable, a symlink, an empty folder, a non-ASCII name).

Junk is left out of the hash, on both sides: `.git`, `.DS_Store`, `__pycache__`, `*.pyc`, `Thumbs.db`, and the local-state folders `node_modules`, `.venv`, `venv`.

The `npx skills` installer also leaves files out when it copies a skill: every `metadata.json`, and folders named `.git`, `__pycache__` and `__pypackages__`; symlinks it copies as the files they point to. So a clean CLI install of an upstream folder that has a `metadata.json` hashes differently from upstream without anyone having edited it. skill-update therefore also computes what the CLI would have left on disk and accepts either: an install identical to upstream or to the CLI's view of it is *current*. When the CLI would not reproduce the folder exactly, an unexplained difference is reported as outdated but not as *edited by you*, because it could just be the omission.

For one lock entry and one installed copy there are three hashes: `installed`, the lock's `skillFolderHash` (what was installed) and `upstream` (the newest). They give the state:

| Condition | State |
|---|---|
| `installed` equals upstream, or what the CLI installs from upstream | current |
| `upstream == lock` and `installed != upstream` | current, with a note: you edited it and upstream has not moved |
| `installed == lock` | outdated (a clean older copy) |
| otherwise, SKILL.md `name:` matches the entry | outdated, and **modified** (edited by hand, or drifted from the lock) unless the CLI omits files from this skill |
| otherwise | unrelated: same folder name, different skill. Left alone. |

The lock file alone is not trusted. It is common for it and the files on disk to disagree, so the answer comes from the files. The lock file is found the way the CLI finds it: `$XDG_STATE_HOME/skills/.skill-lock.json` when that variable is set, otherwise `~/.agents/.skill-lock.json`. An entry without a path inside the repository is reported as unknown (it would otherwise mean the whole repository), and a folder that is a git clone is left to the git channel whatever the lock says.

### One folder name, several lock entries

A repository can publish the same skill twice, for example a generic copy and a Claude-specific one. `npx skills` then records two entries whose names differ only in punctuation (`omd-apply` and `omd:apply`) and installs two folders. Both entries sanitize to the same folder name, so matching by name would pair every folder with both entries. Instead each installed folder is given to the entry it most resembles: an exact hash match with the lock record or with upstream wins, then the number of identical files, then an entry named exactly like the folder.

### Upstream folders that move

When an entry's folder no longer exists upstream, skill-update looks for it again: first a folder whose name matches the skill, then a `SKILL.md` whose `name:` matches (all `SKILL.md` files come down in one batched checkout). If that fails the skill is reported as `unknown`, not guessed at.

## Networking is `git` only

Python's own HTTPS fails on many machines (the python.org macOS build ships no CA bundle: `CERTIFICATE_VERIFY_FAILED`), while `git` works. So every remote operation is a `git` command, and no HTTP client is imported at all.

Each source repository gets a cache under `~/.skill-update/cache/repos/`: a **partial** (`--filter=blob:none`), **shallow** (`--depth=1`), **sparse** clone. A refresh is one `git fetch`. Asking for a folder's hash needs only trees. Only when an update is applied are file contents downloaded: `sparse-checkout set --cone <folders…>` followed by `reset --hard` fetches every needed blob for a source in a single batch.

All values that reach a command line (URLs, refs, folder names, plugin names) are validated first and separated with `--` where git allows it. A lock file containing `--upload-pack=…` is reported as an error, not run.

Names, notes and messages that come from upstream repositories or skill folders are stripped of terminal control characters before they are printed. The identity used to act on an item (a lock key, a plugin id) is kept separately from the cleaned name that is shown.

## Applying an update

For a lock-managed folder:

0. Hash the installed folder again. If it is no longer what the check saw (it was edited, or another tool updated it, while the plan was on screen), skip it.
1. Copy the upstream folder out of the cache into a staging folder.
2. Check that the staged folder hashes to the upstream hash.
3. Move the installed folder into `~/.skill-update/backups/<run>/dirs/` and move the staged folder into its place.
4. Hash the installed folder again. If it differs, move the backup back (complete, `node_modules` included) and report the failure. Only after it passes, move `node_modules` / virtualenvs from the old folder into the new one. A symlink anywhere on that path inside the new folder stops the move, so a hostile upstream cannot aim it elsewhere.
5. After every copy of a skill succeeded, write the new hash into the lock entry (re-read and written atomically through a symlink if the file is one, with a full copy of the file kept). The keys and fields this run changed, with their old and new values, go into the run manifest. Only lock format version 3 is ever written.

Folders only ever move with `rename(2)`, which is atomic: a crash leaves the old folder or the new one, never half of each, and nothing is deleted while it is the only copy. That needs the skill folder, the staging area and the backups on one disk; a skill on another disk is skipped with an explanation instead of being copied. Names that come from lock files are never used as path components (only a sanitized form is).

A skill updating itself is always processed last, so the running tool is not replaced while other work is pending.

For a git clone: stash uncommitted edits (including untracked files) if any, `git merge --ff-only @{u}`, and record old and new HEAD plus the stash in the run manifest. A diverged clone is never touched.

For plugins: `claude plugin marketplace update`, then `claude plugin update <name@marketplace>` per plugin, then compare the registry before and after to learn what really changed. A plugin that its marketplace no longer lists (or has renamed) cannot be updated by anyone, so it is reported under "needs your attention" with the new name if there is one, instead of being attempted.

## Backups and rollback

Each run writes `~/.skill-update/backups/<UTC timestamp>/manifest.json` (before it moves anything, and after each step) listing every directory moved, git repository advanced and lock record changed. `rollback` replays it in reverse:

- a folder: the current folder is set aside and the backup moves back (a run that stopped between its two moves is finished too);
- a git clone: edits made since the update are stashed first, then the clone returns to its old commit and your saved edits are re-applied;
- the lock file: only the entries this run changed are put back, and only if they still hold the values the run wrote, so a skill added or removed since is never clobbered.

The last 10 runs are kept. `--no-backup` deletes replaced folders only after the whole run succeeded (an error or an interruption keeps every backup) and is refused while any skill has local edits.

A lock file (`~/.skill-update/run.lock`, `flock`) keeps two runs from changing things at once.

## What is left alone on purpose

- Git clones outside the skill folders, symlinks into other projects, and skills that live inside a larger repository. Those are somebody's working copies: reported as unmanaged, never fetched.
- Git clones whose `.git` is not a plain folder owned by the current user, or whose `.git/config` sets something that makes git run a program (`core.fsmonitor`, `core.sshCommand`, `core.hooksPath`, credential helpers, `filter.*`, `remote.*.uploadpack`, `include.*`, …). A skill is data a user installed, not permission to run code whenever a maintenance tool looks at it. Every git command also runs with `core.fsmonitor=false`, `core.hooksPath=/dev/null`, `core.pager=cat`, `core.editor=true` and `diff.external=` switched off, so hooks never run.
- Diverged clones.
- Anything with no recorded origin.
- Scripts shipped by a skill. skill-update never executes anything that comes from an update. A git clone that ships a setup script (`setup`, `setup.sh`, `install.sh`, `bootstrap.sh`) is also left out of a plain `update` and listed under `skipped`, because its new files may need that script run again; naming the clone updates it.

## What is not protected

Skill contents are instructions an agent will follow. skill-update checks *where* an update comes from (the same repository the skill was installed from) and shows *what* changes, but it does not judge the contents. Reading a diff before approving it is the user's job.

## JSON output

`check --json`:

```json
{
  "tool": "skill-update", "version": "0.1.0", "command": "check", "elapsed_seconds": 6.7,
  "skill_folders": [{ "agent": "claude-code", "path": "~/.claude/skills", "skills": 73 }],
  "summary": { "outdated": 3, "blocked": 1, "error": 0, "unknown": 2, "current": 40, "unmanaged": 9, "total": 55 },
  "items": [{
    "name": "pdf-tools", "kind": "lock", "status": "outdated", "source": "acme/office-skills",
    "agents": ["universal"], "locations": ["~/.agents/skills/pdf-tools"],
    "installed": "9c1f0aa", "latest": "42be7d1", "modified": true,
    "changes": { "added": 0, "modified": 2, "removed": 0, "files": { "added": [], "modified": ["SKILL.md", "scripts/convert.py"], "removed": [] } },
    "notes": [], "compare_url": "https://github.com/acme/office-skills/commits/HEAD/skills/pdf-tools"
  }],
  "notes": []
}
```

`kind` is `lock`, `git`, `plugin` or `unmanaged`. `status` is `outdated`, `current`, `blocked`, `unknown`, `error` or `unmanaged`. `setup_script` is present (as `true`) only on a git clone that ships a setup script.

`update --json` returns `results[]` (`name`, `kind`, `outcome` = `updated` | `unchanged` | `failed`, `detail`, `notes`), `skipped[]` (`name`, `reason`), `run_id` (null when nothing was backed up) and `notes`.

Exit codes: `0` success; `1` when `check --exit-code` finds updates or an `update` item failed; `2` for usage errors, a missing `--yes` without a terminal, or a run already in progress; `130` when interrupted.

## Decisions

**Apply updates ourselves instead of running `npx skills update`.** The official command only knows what it installed, shows no preview, keeps no backup, overwrites silently and needs Node. Reproducing its lock hashes made it cheap to check natively, and replacing a folder is simple enough to do safely. Afterwards the lock record is refreshed, so the two tools agree.

**Edited skills are replaced after a backup by default.** That is what updating means, and the backup makes it recoverable. `--keep-modified` protects edited skills for people who prefer the opposite default. The policy lives in one small function, `policy.select`.

**A clone with a setup script is skipped unless named.** Packs that build or link things in a `setup` step need it run again after an upgrade. Fast-forwarding the files without that step can leave a half-updated pack, and skill-update must not run the script itself. So a plain `update` leaves such a clone alone and says why; `update NAME` is the explicit go-ahead, and its result reminds you to run the script. Same rule as `--keep-modified`: naming a skill overrides a default skip.

**Plugins are delegated.** Claude Code owns plugin state, and half of the plugin catalogs publish no version. We check what can be known and let `claude plugin update` decide the rest.

**No telemetry, no HTTP client, no dependencies.** The tool must be safe to run on any machine and trivial to audit.

## Known limits

- Category folders that nest skills one level deeper (Hermes) are not scanned.
- Project-level skills (`./.claude/skills` inside a repository) are out of scope; pass `--root` to include one.
- Windows is untested.
- Lock file versions other than 3 are read but never written.
- When two lock entries match one folder equally well (the same content published at two paths), the one that sorts first wins and the other is not listed.
- A lock entry that cannot be resolved (offline, unsupported source) does not compete for a folder, so a resolvable entry with the same folder name can take it.
- git older than 2.30 is not supported; the sparse checkout is done by writing the pattern file directly so that 2.30 to 2.34 work, but only recent versions are tested.
