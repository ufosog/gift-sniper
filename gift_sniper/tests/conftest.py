import json
import os
import sys
from pathlib import Path

os.environ.setdefault("PORTALS_AUTH", "test-token-do-not-use")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    with open(FIXTURES_DIR / name, encoding="utf-8") as f:
        return json.load(f)
