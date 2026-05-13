"""Allow `python -m o7k.expediter [package ...]` invocation."""

import sys
from o7k.expediter.expediter import run

if __name__ == "__main__":
    packages = sys.argv[1:] or None
    sys.exit(run(packages))
