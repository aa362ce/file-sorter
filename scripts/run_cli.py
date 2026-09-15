"""PyInstaller entry point for the file-sorter CLI.

file_sorter/cli.py uses relative imports (it's part of a package), so
PyInstaller can't point directly at it -- this thin wrapper does an
absolute import instead, which both PyInstaller and plain `python
scripts/run_cli.py` can run.
"""

import sys

from file_sorter.cli import main

if __name__ == "__main__":
    sys.exit(main())
