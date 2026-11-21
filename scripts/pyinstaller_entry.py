"""PyInstaller entry point.

Uses an absolute import so the frozen app resolves the package.
"""

from pahebatcher import main

if __name__ == "__main__":
    main.main()
