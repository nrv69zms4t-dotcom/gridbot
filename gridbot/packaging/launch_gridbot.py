"""PyInstaller entry point for GridBot.

gridbot/app/main.py uses relative imports (it is a package module, run as
``python -m gridbot.app``).  PyInstaller executes its entry script as a
top-level module, where relative imports fail with
``ImportError: attempted relative import with no known parent package``.

This thin launcher imports the package module absolutely, so modulegraph
sees the full gridbot.app dependency tree (api, engine, pandas, ...) and
relative imports inside the package work as usual.
"""

import sys

from gridbot.app.main import main

if __name__ == "__main__":
    sys.exit(main())
