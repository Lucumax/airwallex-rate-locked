"""One command, no install, no keys: python scripts/demo.py [--out prototype]"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rate_locked.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["demo", *sys.argv[1:]]))
