from __future__ import annotations

import os
from pathlib import Path
import sys


def _use_executable_dir_as_cwd() -> None:
    if getattr(sys, "frozen", False):
        os.chdir(Path(sys.executable).resolve().parent)


def main() -> int:
    _use_executable_dir_as_cwd()
    from lagrange_bot.gui import main as gui_main

    return int(gui_main())


if __name__ == "__main__":
    raise SystemExit(main())
