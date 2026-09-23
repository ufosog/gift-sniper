"""report.py never touches the network -- it must be importable and
usable without PORTALS_AUTH set at all. This has to run in a genuinely
clean subprocess: conftest.py sets PORTALS_AUTH via os.environ.setdefault
for the rest of the suite, so an in-process test would not catch a
regression here.
"""
import subprocess
import sys
from pathlib import Path


def test_import_report_without_portals_auth_does_not_raise():
    import os

    env = {k: v for k, v in os.environ.items() if k != "PORTALS_AUTH"}
    result = subprocess.run(
        [sys.executable, "-c", "import gift_sniper.report"],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr


def test_import_config_without_portals_auth_does_not_raise():
    import os

    env = {k: v for k, v in os.environ.items() if k != "PORTALS_AUTH"}
    result = subprocess.run(
        [sys.executable, "-c", "import gift_sniper.config"],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr


def test_auth_manager_works_without_portals_auth():
    """PORTALS_AUTH is optional since 2026-09-19: every Portals endpoint
    the project calls answers anonymous requests with the same content
    (diag/portals_noauth_probe.py). AuthManager() without it must work.
    """
    import os

    env = {k: v for k, v in os.environ.items() if k != "PORTALS_AUTH"}
    result = subprocess.run(
        [
            sys.executable, "-c",
            "from gift_sniper.auth import AuthManager\n"
            "try:\n"
            "    AuthManager()\n"
            "    print('NO_ERROR')\n"
            "except Exception as e:\n"
            "    print(type(e).__name__)\n",
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        env=env,
    )
    assert "NO_ERROR" in result.stdout


def test_no_token_sends_no_authorization_header():
    from gift_sniper.portals_client import PortalsClient
    assert "Authorization" not in PortalsClient(auth_provider=lambda: "")._headers()
    assert PortalsClient(auth_provider=lambda: "abc")._headers()["Authorization"] == "tma abc"


def test_db_paths_are_absolute_by_default(monkeypatch):
    """Found by the daily review 2026-09-19: `python health.py` run from
    another directory died with "unable to open database file" while the
    same code inside the supervisor worked."""
    import os
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if k not in ("DB_DSN", "JOURNAL_DB_DSN")}
    result = subprocess.run(
        [sys.executable, "-c",
         "from gift_sniper import config, journal_config\n"
         "import os\n"
         "print(os.path.isabs(config.DB_DSN), os.path.isabs(journal_config.JOURNAL_DB_DSN))\n"
         "print(os.path.basename(config.DB_DSN), os.path.basename(journal_config.JOURNAL_DB_DSN))"],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True,
        env={**env, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
    )
    assert "True True" in result.stdout, result.stderr
    assert "gift_sniper.db journal.db" in result.stdout


def test_explicit_env_dsn_still_wins():
    """Run in a subprocess: re-importing config in-process would replace the
    module other tests already hold a reference to."""
    import os
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-c", "from gift_sniper import config, journal_config;"
                               "print(config.DB_DSN, journal_config.JOURNAL_DB_DSN)"],
        cwd=root, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(root), "DB_DSN": "other.db", "JOURNAL_DB_DSN": "other-journal.db"},
    )
    assert result.stdout.strip() == "other.db other-journal.db", result.stderr
