# Contributing

Thanks for helping. A few things that keep this tool safe to run on other people's machines:

- **Standard library only**, Python 3.9+. No HTTP client: anything remote goes through `git`.
- **Nothing is changed without a backup**, and every change is verified afterwards.
- **Tests are sandboxed.** Each test gets its own `HOME` and local bare git repositories; none may touch the developer's real skills. Run them with:

  ```bash
  python3 -m unittest discover -s tests -v
  ```

- New behaviour needs a test that fails without it. `tests/helpers.py` has `Upstream` (a local stand-in for a GitHub repository) and `Sandbox` (a throw-away machine).
- **Windows is tested in CI**, so keep tests portable: `self.program(...)` instead of a shebang script, `self.symlink(...)` instead of `os.symlink`, `.as_posix()` for paths that go into git config. `SKILL_UPDATE_LENIENT=1` runs the Windows comparison rules on any platform. `tools/e2e_npx_skills.py` installs with the real `npx skills` into a throw-away home (needs Node and network).

## Where things live

| Want to… | Look at |
|---|---|
| support another agent's skill folder | `KNOWN_ROOTS` in `skills/skill-update/scripts/skillupdate/discover.py` |
| change what gets updated by default | `policy.py` |
| change the lock-file logic | `lockfile.py` (states are documented in `docs/DESIGN.md`) |
| change how folders are compared (also on Windows) | `treehash.py`, `Scan.view` |
| change the output | `report.py`, then `python3 tools/make_demo.py` to refresh the screenshots |

## Reporting problems

Please include the output of `python3 skill_update.py scan` and `check --json` with your home folder replaced (the tool already prints `~`). Do not paste lock files that contain private repository URLs.
