"""КАК ТЕСТИРОВАТЬ 6, 7, 8, 10 (paper journal in the bot): /magazine,
/magazine_full, /magazine_reset.
"""
from datetime import timedelta

import pytest

from gift_sniper import db, journal_config, journal_db, paper_journal
from gift_sniper.journal_view import magazine_text
from gift_sniper.notifier import CommandHandler, TelegramNotifier
from gift_sniper.paper_journal import JournalClients, record_signal, run_closer
from .test_notifier import FakeSession
from .test_paper_journal import FakePortals, T0, _sig

OK = (200, {"ok": True, "result": {}})


def _updates(*messages):
    return (200, {"ok": True, "result": [
        {"update_id": i + 1, "message": {"from": {"id": sender}, "text": text}}
        for i, (sender, text) in enumerate(messages)
    ]})


def _handler(session, jconn, clock=None):
    notifier = TelegramNotifier("tok", "OWNER", viewer_chat_ids=["111"], session=session)
    return CommandHandler(
        notifier, db.connect(":memory:"), owner_id="OWNER", viewer_ids=["111"],
        journal_conn=jconn, clock=clock or (lambda: 0.0),
    )


def _replies(session):
    return [(data["chat_id"], data["text"]) for _url, data in session.calls[1:]]


@pytest.fixture
def trading_journal(monkeypatch):
    """base scenario: one closed trade (+6.89) and one open position
    bought at 30.00, currently marked at 38.00."""
    monkeypatch.setattr(paper_journal, "draw", lambda sid, scenario: 0.0)
    jconn = journal_db.connect(":memory:")
    clients = JournalClients(portals=FakePortals())
    record_signal(jconn, _sig(ext_id="L1"))
    run_closer(jconn, clients, now=T0 + timedelta(seconds=60))
    run_closer(jconn, clients, now=T0 + timedelta(hours=49))
    second_at = T0 + timedelta(hours=49)
    record_signal(jconn, _sig(ext_id="L2", observed_at=second_at))
    run_closer(jconn, clients, now=second_at + timedelta(seconds=60))
    return jconn


# --- 6: access -----------------------------------------------------------------

def test_item6_owner_viewer_get_summary_stranger_denied():
    jconn = journal_db.connect(":memory:")
    session = FakeSession([_updates(("OWNER", "/magazine"), (111, "/magazine"), (999, "/magazine")), OK, OK, OK])
    _handler(session, jconn).poll_once()

    replies = _replies(session)
    assert replies[0][0] == "OWNER" and "ЖУРНАЛ" in replies[0][1]
    assert replies[1][0] == "111" and replies[1][1] == replies[0][1]
    assert replies[2] == ("999", "доступ ограничен")


def test_magazine_when_journal_disabled():
    session = FakeSession([_updates(("OWNER", "/magazine")), OK])
    _handler(session, None).poll_once()
    assert "журнал выключен" in _replies(session)[0][1]


# --- 7: no trades yet ---------------------------------------------------------------

def test_item7_no_closed_trades_says_so_without_division_by_zero():
    text = magazine_text(journal_db.connect(":memory:"))
    assert "сделок пока нет" in text
    assert "Последние сделки" not in text
    assert "Банк: 100.00 TON" in text
    assert "Сейчас: 100.00 TON  (+0.00%)" in text
    assert "Открыто позиций: 0" in text


# --- 8: reset ---------------------------------------------------------------------------

def test_item8_viewer_reset_refused(trading_journal):
    session = FakeSession([_updates((111, "/magazine_reset")), OK])
    _handler(session, trading_journal).poll_once()
    assert "только владельцу" in _replies(session)[0][1]
    assert trading_journal.execute("SELECT COUNT(*) FROM journal_signals").fetchone()[0] > 0


def test_item8_owner_first_reset_asks_confirmation_second_within_minute_resets(trading_journal):
    ticks = iter([0.0, 30.0])
    session = FakeSession([_updates(("OWNER", "/magazine_reset")), OK, _updates(("OWNER", "/magazine_reset")), OK])
    handler = _handler(session, trading_journal, clock=lambda: next(ticks))

    handler.poll_once()
    assert "Для подтверждения" in _replies(session)[0][1]
    assert trading_journal.execute("SELECT COUNT(*) FROM journal_signals").fetchone()[0] > 0

    handler.poll_once()
    assert session.calls[3][1]["text"] == "журнал обнулён"
    assert trading_journal.execute("SELECT COUNT(*) FROM journal_signals").fetchone()[0] == 0
    balances = {r[0] for r in trading_journal.execute("SELECT balance_nano FROM journal_state")}
    assert balances == {journal_config.limits(s)[0] for s in journal_config.SCENARIOS}


def test_reset_confirmation_expires_after_a_minute(trading_journal):
    ticks = iter([0.0, 61.0])
    session = FakeSession([_updates(("OWNER", "/magazine_reset")), OK, _updates(("OWNER", "/magazine_reset")), OK])
    handler = _handler(session, trading_journal, clock=lambda: next(ticks))
    handler.poll_once()
    handler.poll_once()
    assert "Для подтверждения" in session.calls[3][1]["text"]
    assert trading_journal.execute("SELECT COUNT(*) FROM journal_signals").fetchone()[0] > 0


# --- 10: realized vs unrealized ---------------------------------------------------------------

def test_item10_realized_and_open_estimate_shown_separately(trading_journal):
    text = magazine_text(trading_journal)
    assert "Сделок закрыто: 1  ·  прибыльных: 1 (100%)" in text
    assert "Прибыль по закрытым: +6.89 TON (+6.9% от банка)" in text
    assert "Оценка открытых: +8.00 TON (+8.0% от банка)" in text
    assert "Сейчас: 114.89 TON  (+14.89%)" in text  # 100 + 6.89 realized + 8.00 open estimate
    assert "куплен 30.00, сейчас 38.00" in text
    assert "CollA #7  30.00 -&gt; 38.00  +6.89" not in text  # no escaping of the arrow
    assert "CollA #7  30.00 -> 38.00  +6.89" in text


def test_magazine_full_adds_marketplace_and_floor_level_split(trading_journal):
    text = magazine_text(trading_journal, full=True)
    assert "По площадкам:" in text
    assert "· portals: закрыто 1 · прибыльных 1 (100%) · прибыль +6.89 TON · открыто 1" in text
    assert "По уровню флора:" in text
    assert "· pair: закрыто 1" in text


def test_trade_list_is_cut_before_the_summary(trading_journal):
    full_text = magazine_text(trading_journal)
    summary = full_text.split("\n\nПоследние сделки:")[0]
    short = magazine_text(trading_journal, limit=len(summary) + 5)
    assert short == summary


def test_magazine_full_command_from_viewer(trading_journal):
    session = FakeSession([_updates((111, "/magazine_full")), OK])
    _handler(session, trading_journal).poll_once()
    assert "По уровню флора:" in _replies(session)[0][1]
