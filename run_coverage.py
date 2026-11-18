"""
Run the test-suite under coverage and print the report - with the interpreter
you start it with:

    python run_coverage.py              # report to the console
    python run_coverage.py --html       # + htmlcov/index.html

If the ``coverage`` package is missing for this interpreter it is installed
first (``python -m pip install coverage``), so the command works right after
a fresh checkout.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main(argv: list[str]) -> int:
    if importlib.util.find_spec("coverage") is None:
        print(f"coverage is not installed for {sys.executable} - installing it ...", flush=True)
        code = subprocess.call([sys.executable, "-m", "pip", "install", "coverage>=7.0"])
        if code != 0:
            print("could not install coverage; run: python -m pip install -r requirements.txt", file=sys.stderr)
            return code
    run = [sys.executable, "-m", "coverage", "run", "--source=stock_dataload", "-m", "unittest", "discover",
           "-s", "tests", "-t", "."]
    code = subprocess.call(run, cwd=ROOT)
    subprocess.call([sys.executable, "-m", "coverage", "report", "-m"], cwd=ROOT)
    if "--html" in argv:
        subprocess.call([sys.executable, "-m", "coverage", "html"], cwd=ROOT)
        print(f"HTML report: {ROOT / 'htmlcov' / 'index.html'}")
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
