"""Entry script used by PyInstaller (see scripts/build_exe.bat).

Keeping it separate means the frozen build and the source build start in
exactly the same way: both call main.main().
"""

from __future__ import annotations

import multiprocessing
import sys


def run() -> int:
    from main import main

    return main(sys.argv[1:])


if __name__ == "__main__":
    # Required for a frozen Windows build: without it, anything that spawns a
    # process would re-run the application.
    multiprocessing.freeze_support()
    raise SystemExit(run())
