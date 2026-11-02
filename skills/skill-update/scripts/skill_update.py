#!/usr/bin/env python3
"""Entry point: python3 skill_update.py [check|update|rollback|scan] [options]"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from skillupdate.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
