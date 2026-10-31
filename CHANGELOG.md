# Changelog

## 0.1.0

First release.

- `check`, `update`, `rollback` and `scan`, with `--json` output for agents.
- Skills installed with `npx skills` are compared with the newest upstream folder by git tree hash, and updated with a backup, a verification and a refreshed lock record.
- Git clones in skill folders are fast-forwarded; uncommitted edits go to `git stash`.
- Claude Code plugins are checked against marketplace catalogs and updated through `claude plugin update`.
- Looks in 67 known skill folders of 66 agents.
- Backups, rollback, dry run, `--keep-modified`, `--ignore`, `--root`.
- Network access only through `git`; no dependencies, no telemetry.
