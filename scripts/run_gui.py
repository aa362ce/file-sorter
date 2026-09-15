"""PyInstaller entry point for the file-sorter GUI. See run_cli.py for why
this wrapper exists instead of pointing PyInstaller at the package directly.
"""

from file_sorter.gui.main import run

if __name__ == "__main__":
    run()
