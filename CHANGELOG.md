# Changelog

## Unreleased

- Runs natively on Windows 10 and later. CI now tests Windows too, and every platform runs a real `npx skills` install, update and rollback (`tools/e2e_npx_skills.py`).
  - Where the file system has no executable bit and line endings were converted to CRLF (Windows by default), folders are compared the way git would hold them, so installed skills are not reported as edited.
  - A second run is refused with a Windows file lock; a folder that a program holds open is retried and then explained; read-only git objects can be removed; paths show a `~` and forward slashes; output is UTF-8; the console is switched to ANSI colors.
- The repository cache ignores the user's line-ending and long-path settings, so `core.autocrlf=true` no longer makes an update fail its verification. macOS and Linux users with that setting were affected too.
- An unedited older copy is no longer called edited on Windows when a script changed, was removed or flipped its executable bit between two versions.
- `git` and `claude` are looked up on `PATH` only, never in the current folder, where Windows searches first; a `.cmd` or `.bat` wrapper is refused for git.
- A `file://` address must name no host and a local path must not start with `//` (git for Windows would read them as a network share). Folder names from a repository are checked against Windows' rules (drive letters, device names, trailing dots). Backslashes in a lock file's `skillPath` read as slashes; before, such a path meant the whole repository.
- The repository cache ignores `.gitattributes`, so `text`, `eol` and filters cannot rewrite the files it checks out. Files are written with Unix line endings, a byte order mark no longer hides a skill's name, and a link or junction inside a skill is never followed.
- `desktop.ini` is ignored like `Thumbs.db`.
- `skill_update.py` is executable in the repository, like the script it is.

## 0.1.1

- A git clone that ships a setup script (`setup`, `setup.sh`, `install.sh` or `bootstrap.sh`) is now left out of a plain `update` and listed under `skipped` with the reason, because its new files may need that script run again. Name it (`update NAME`) to update it. The script is still never run. `check --json` marks such clones with `setup_script: true`.
- The update plan tells skipped copies of one skill apart by folder, and the README screenshots show the current output; a test now keeps the check screenshot in step with the version.

## 0.1.0

First release.

- `check`, `update`, `rollback` and `scan`, with `--json` output for agents.
- Skills installed with `npx skills` are compared with the newest upstream folder by git tree hash, and updated with a backup, a verification and a refreshed lock record.
- Git clones in skill folders are fast-forwarded; uncommitted edits go to `git stash`.
- Claude Code plugins are checked against marketplace catalogs and updated through `claude plugin update`.
- Looks in 67 known skill folders of 66 agents.
- Backups, rollback, dry run, `--keep-modified`, `--ignore`, `--root`.
- Network access only through `git`; no dependencies, no telemetry.
