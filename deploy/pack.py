"""Run on the Windows machine, from the project folder, AFTER stopping
start.ps1 (Ctrl+C):   python deploy/pack.py
Makes gift-sniper-pack.tar.gz: code, consistent copies of both databases
(sqlite backup API, WAL included), gift-sniper.env converted from env.ps1.
"""
import re
import sqlite3
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "gift-sniper-pack.tar.gz"


def env_from_ps1(text: str) -> str:
    lines = []
    for m in re.finditer(r'^\s*\$env:([A-Z0-9_]+)\s*=\s*"([^"]*)"', text, re.M):
        lines.append(f"{m.group(1)}={m.group(2)}")
    lines += ["LOG_DIR=logs", "PYTHONIOENCODING=utf-8"]
    return "\n".join(lines) + "\n"


def main():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for name in ("gift_sniper.db", "journal.db"):
            src = sqlite3.connect(f"file:{ROOT / name}?mode=ro", uri=True)
            dst = sqlite3.connect(tmp / name)
            src.backup(dst)
            dst.close()
            src.close()
        (tmp / "gift-sniper.env").write_text(
            env_from_ps1((ROOT / "env.ps1").read_text(encoding="utf-8-sig")), encoding="utf-8")
        with tarfile.open(OUT, "w:gz") as tar:
            tar.add(ROOT / "gift_sniper", "gift_sniper",
                    filter=lambda t: None if "__pycache__" in t.name or ".pytest_cache" in t.name else t)
            for f in ("health.py", "ARCHITECT.md"):
                tar.add(ROOT / f, f)
            tar.add(ROOT / "deploy", "deploy")
            secrets = ROOT / "secrets"
            if secrets.exists():
                tar.add(secrets, "secrets")
            for f in ("gift_sniper.db", "journal.db", "gift-sniper.env"):
                tar.add(tmp / f, f)
    print(f"Готово: {OUT} ({OUT.stat().st_size / 1e6:.0f} МБ)")


if __name__ == "__main__":
    main()
