# Changelog

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
