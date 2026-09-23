"""Telegram notifications for clean signals (see signals.py for exactly
what counts as "clean" -- this module never re-derives that logic).
HTTP-only via requests, no external Telegram library dependency, same
approach as portals_client.py. TELEGRAM_BOT_TOKEN is NEVER logged,
matching PORTALS_AUTH's handling throughout this codebase (see auth.py).

This is a PERSONAL tool, now with two roles (see config.py's
TELEGRAM_OWNER_ID / TELEGRAM_VIEWER_IDS): the OWNER receives every
notification and can run every command; VIEWERS also receive every
notification but can only run read-only commands (/status, /last).
Anyone else is rejected -- there is still no public access, by design.
"""
from __future__ import annotations

import html
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import requests

from . import config, db
from .cross_check import VERDICT_SENT_NEIGHBOUR_HIGHER
from .signals import Signal

logger = logging.getLogger("gift_sniper.notifier")

TELEGRAM_API_BASE = "https://api.telegram.org"
MAX_RETRY_ATTEMPTS = 5

# CONFIRMED live, per-lot deep link into the Portals market:
#   https://t.me/portals_market_bot/market?startapp=gift_<external_id>
# opens the specific lot's card, with a buy button. external_id is the
# listing's own "id" field (a UUID), already stored per row. Confirmed
# the suffix seen in an original share link (e.g. "_mpb14p") is NOT
# required -- the link works with just gift_<external_id>.
#
# This SUPERSEDES the prior conclusion (previous delivery) that no
# per-lot deep link exists: that conclusion was based on only seeing the
# calling side of the mini-app's share-link code
# (formatShareNftLink) in the reviewed bundle chunk; the function body
# living in a different chunk was missed. The bundle's `startapp`
# content genuinely does support a "gift_" prefix in addition to
# "collection-" (see the retired _portals_collection_link, no longer
# used now that the precise lot link exists).
PORTALS_DEEP_LINK_CONFIRMED = True


class TelegramError(RuntimeError):
    pass


def _nft_link(tg_id: str) -> str:
    return f"https://t.me/nft/{tg_id}"


def _portals_lot_link(external_id: str) -> str:
    """Opens this EXACT lot's card in the Portals market mini-app, with
    a buy button -- see PORTALS_DEEP_LINK_CONFIRMED above.
    """
    return f"https://t.me/portals_market_bot/market?startapp=gift_{external_id}"


# CONFIRMED live, per-lot deep link into the Tonnel market mini-app
# (Tonnel signals delivery): https://t.me/tonnel_network_bot/gift?startapp=<gift_id>
# -- opens the specific lot's card. Checked on two lots, including one
# ALREADY SOLD -- the card opens either way. Differs from Portals in two
# ways, both deliberate, neither a typo: the path is /gift, not /market;
# and startapp carries the BARE gift_id with NO "gift_" prefix (Portals'
# startapp uses "gift_<uuid>" -- Tonnel's own gift_id needs none).
# gift_id IS this project's Tonnel external_id (external_id = str(gift_id)).
def _tonnel_lot_link(external_id: str) -> str:
    return f"https://t.me/tonnel_network_bot/gift?startapp={external_id}"


# CONFIRMED live, per-lot deep link into the MRKT mini-app (MRKT
# full-signaller delivery): https://t.me/mrkt/app?startapp=<id-no-dashes>
# -- confirmed id "4c667e31-e667-40ed-a41d-641791998bb9" ->
# startapp=4c667e31e66740eda41d641791998bb9. external_id IS the lot's
# `id` (a UUID, this project's MRKT external_id) -- dashes stripped, no
# other transformation.
def _mrkt_lot_link(external_id: str) -> str:
    return f"https://t.me/mrkt/app?startapp={external_id.replace('-', '')}"


def format_caption(
    signal: Signal, resend_after_drop: bool = False, test_send: bool = False, prefix: str | None = None
) -> str:
    """HTML message text -- deliberately minimal, exactly 5 content lines
    plus 2 blank separators, nothing the user has to interpret:

        ЛИСТИНГ ✓

        <title>
        <traits>

        PRICE: <price> <currency>
        FLOOR: <floor> <currency>

    FLOOR is the last visible line.

    Правка 1/2 (unified notification delivery): NO marketplace-specific
    label ("ЛИСТИНГ TONNEL" vs plain "ЛИСТИНГ") -- the user does not need
    to know which marketplace a signal came from, only that it's worth
    buying (per spec: "нажать кнопку и купить").

    ДЕФЕКТ 5 (systemic-check delivery): the checkmark is CONDITIONAL
    again, but on a DIFFERENT thing than before. Between Правка 1/2 and
    this delivery it was unconditional ("ЛИСТИНГ ✓" always) -- that was
    the right call AT THE TIME because the checkmark used to mean
    cross_verdict == "confirmed", a LEGACY verdict from when cross-check
    was a separate SIGNAL TYPE, not a pre-send filter; the same lot could
    get one verdict as a Portals signal and a contradictory one as a
    cross-market signal (Victory Medal #86056: rejected verdict="worse"
    as a Portals signal AND simultaneously sent as a cross-market one).
    Making cross-check a pure pre-send filter (still true today -- see
    cross_check.py / poller.py's _maybe_cross_check) removed that
    contradiction, so the checkmark became meaningless as a gate: EVERY
    signal reaching this function had already passed the same filter,
    confirmed or not.

    But "passed the filter" and "an independent neighbour actually
    confirmed a higher price exists" are two different facts, and only
    the checkmark ever claimed the second one. Measured live: 62/102
    (61%) of cross-check snapshots were sent_no_neighbour (no comparable
    listing on ANY other marketplace at all) or neighbour_thin (25/102,
    data existed but was discarded) -- a signal with genuinely zero
    corroborating evidence looked IDENTICAL in Telegram to one an
    independent neighbour actively confirmed. The checkmark now reflects
    exactly that distinction, nothing about whether the signal was
    blocked (nothing reaching this function was ever blocked, unchanged):
    "ЛИСТИНГ ✓" only when cross_verdict == sent_neighbour_higher (a real,
    independent confirmation -- including the new two-neighbour-
    agreement vote, see cross_check.py's CROSS_AGREEMENT_PCT, which
    ALSO produces sent_neighbour_higher when the agreed price clears
    CROSS_MIN_GAP_PCT); plain "ЛИСТИНГ" (no checkmark) for
    sent_no_neighbour, neighbour_thin, error, or not_checked
    (CROSS_CHECK_ENABLED=False, or no (collection,model,backdrop) triple
    to compare).

    Per spec: "убрать из уведомления всё, что требует от пользователя
    думать. Софт считает — пользователь получает решение." Every field
    dropped here (delta%, old price, listed_count, liquidity, profit,
    the model-floor caveat, which marketplace, cross-check detail) is
    NOT dropped anywhere else -- signals.py's filter cascade (thin-book,
    is_implausible, is_bulk_update, ratio, the pre-send freshness check,
    cross_check.py's neighbour-price filter, ...) is exactly what
    already vetted this signal before it got here, and report.py still
    prints all of it for diagnostics. This function only controls what
    the Telegram message SHOWS.

    The title is PLAIN bold text -- NOT a link (a blue-colored title is
    unwanted, per spec).

    The compact-animation experiment (sendAnimation/sendSticker/
    sendDocument, a preceding delivery) is CLOSED with a negative result
    -- see README "closed question": Telegram rejected the gift's
    .lottie.json as both animation and sticker content, and as a
    document it delivered a raw 340KB FILE into the chat
    (khabibspapakha-5245.lottie.json) instead of any kind of card.
    lottie renders ONLY inside Telegram's own native gift-card UI, which
    cannot be invoked directly by this bot. Those three send methods are
    REMOVED from this codebase entirely (see TelegramNotifier.send_signal)
    so a raw file can never be sent again. The ONLY remaining approach is
    Telegram's own link-preview unfurl.

    CLOSED QUESTION -- a zero-width anchor (link on a trailing space with
    no visible character) does NOT reliably trigger the unfurl: confirmed
    twice live (Diamond Ring #23673, then again Fine Pen #11525) that the
    exact same message/link/og-tags sometimes unfurls and sometimes
    doesn't when the link sits on an invisible character, while switching
    to a VISIBLE anchor character fixed it both times. See README -- do
    not revert to a zero-width anchor.

    The link is instead attached to a visible "·" character appended
    INLINE at the end of the FLOOR line (not the title, so the title
    stays plain, non-blue text; not a separate line, so the message
    doesn't grow a line the user has to interpret -- the "·" reads as
    part of the line, not as an inserted element). Per spec (Правка 2,
    item 3), this link is mandatory in every unified-format notification
    -- when tg_id is absent (rare, a data gap upstream, not a signal
    type distinction) FLOOR falls back to a plain line with no link.

    `resend_after_drop`: True when this signal is a cooldown-exempt resend
    (SIGNAL_RESEND_DROP_PCT cleared -- see config.py / poller.py) -- the
    header gets an explicit "· ЦЕНА СНИЖЕНА" marker so the user
    immediately understands why they're seeing this listing again so
    soon after the last one.

    `test_send`: True only for gift_sniper/send_test_signals.py -- appends
    a "— тестовая отправка" line at the very end (after the link line),
    so a manual formatting check is never mistaken for a real signal.

    `prefix`: used only by gift_sniper/preview_ab_test.py -- prepended as
    its own first line (e.g. "[A] 3"), so a human counting cards in
    Telegram afterward can tell which A/B group and ordinal each message
    belongs to. None (the default) adds nothing -- every other caller is
    unaffected.

    All dynamic text is html.escape()'d -- collection/model/backdrop/
    symbol names come from the marketplace API and must never be trusted
    not to contain HTML-significant characters (Telegram's HTML
    parse_mode would either reject the message or render broken markup
    otherwise).
    """
    title = html.escape(f"{signal.collection_name or '?'} #{signal.gift_number or '?'}")
    currency = html.escape(signal.currency)

    traits = [t for t in (signal.model_name, signal.backdrop_name, signal.symbol_name) if t]
    traits_line = " · ".join(html.escape(t) for t in traits)

    new_price = Decimal(signal.new_price_nano) / config.NANO
    floor = Decimal(signal.floor_nano) / config.NANO

    # ДЕФЕКТ 5: checkmark only for a REAL independent neighbour
    # confirmation (cross_verdict == sent_neighbour_higher) -- see this
    # function's docstring for why this is different from the old,
    # removed "confirmed"-gated checkmark.
    header_text = "ЛИСТИНГ ✓" if signal.cross_verdict == VERDICT_SENT_NEIGHBOUR_HIGHER else "ЛИСТИНГ"
    if resend_after_drop:
        header_text += " · ЦЕНА СНИЖЕНА"
    header = f"<b>{header_text}</b>"
    lines = [
        header,
        "",
        f"<b>{title}</b>",
    ]
    if traits_line:
        lines.append(traits_line)
    floor_line = f"FLOOR: {floor:.2f} {currency}"
    if signal.tg_id:
        # Visible-character anchor at the end of the FLOOR line -- NOT a
        # zero-width anchor (confirmed unreliable twice live, see
        # docstring above / README) and NOT a separate line (the link
        # must not look like an inserted element).
        floor_line += f' <a href="{_nft_link(signal.tg_id)}">·</a>'

    lines += [
        "",
        f"PRICE: {new_price:.2f} {currency}",
        floor_line,
    ]

    if test_send:
        lines.append("— тестовая отправка")

    if prefix:
        lines.insert(0, html.escape(prefix))

    return "\n".join(lines)


def build_keyboard(signal: Signal) -> dict:
    """Exactly ONE button -- straight to this lot's buy page. Правка 2/4:
    the button text is just "Купить" for EVERY marketplace -- the user
    doesn't need to know which marketplace the signal came from, only
    that pressing it buys the lot. The URL itself still differs per
    marketplace -- only the visible label is unified.
    """
    text = "Купить"
    if signal.marketplace == "tonnel":
        return {"inline_keyboard": [[{"text": text, "url": _tonnel_lot_link(signal.listing_external_id)}]]}
    if signal.marketplace == "mrkt":
        return {"inline_keyboard": [[{"text": text, "url": _mrkt_lot_link(signal.listing_external_id)}]]}
    return {"inline_keyboard": [[{"text": text, "url": _portals_lot_link(signal.listing_external_id)}]]}


def passes_notify_threshold(signal: Signal) -> bool:
    """The gate applied per signal level -- NOTIFY_MIN_PROFIT_USD only
    ever applies to level="pair" signals (the only ones with a real,
    non-estimated profit_usd; see signals.Signal.profit_is_estimate and
    signals.compute_profit_nano's docstring on why level="model" profit
    is an estimate against a different backdrop's price). level="model"
    signals are gated on NOTIFY_MIN_DISCOUNT_PCT alone -- discount%,
    unlike absolute profit, is a valid comparison at both levels. Every
    surviving signal already cleared MIN_SIGNAL_PROFIT_TON in the
    cascade's below_min_profit stage, at both levels.
    """
    if not signal.profit_is_estimate:
        return signal.profit_usd is not None and signal.profit_usd >= config.NOTIFY_MIN_PROFIT_USD
    return (signal.discount * 100) >= config.NOTIFY_MIN_DISCOUNT_PCT


def _priority(signal: Signal) -> tuple:
    """Ranking key for select_signals_to_send: real, confirmed profit_usd
    (level=pair, not an estimate) outranks a level=model signal (whose
    profit_usd is only an estimate against a different backdrop) --
    within each tier, higher profit_usd / higher discount% wins.
    """
    if not signal.profit_is_estimate and signal.profit_usd is not None:
        return (2, signal.profit_usd)
    return (1, signal.discount)


def select_signals_to_send(signals_list: list[Signal], max_count: int) -> tuple[list[Signal], int]:
    """Telegram limits ~30 msg/sec globally and ~20/min per chat
    (confirmed in the Bot API docs). If more clean signals arrive in one
    check than NOTIFY_MAX_PER_MINUTE, send the MOST PROFITABLE/highest-
    discount `max_count` (see _priority) and summarize the rest in one
    line -- never silently drop a signal, never flood the chat. Returns
    (to_send, extra_count).
    """
    if len(signals_list) <= max_count:
        return list(signals_list), 0
    ranked = sorted(signals_list, key=_priority, reverse=True)
    return ranked[:max_count], len(signals_list) - max_count


class TelegramNotifier:
    """Sends clean-signal notifications. Every public send method is
    wrapped so a Telegram-side failure (network error, 4xx, exhausted
    429 retries) is logged and returns False/[] -- it must NEVER raise
    up into poller.py, which would take the whole poll loop down over a
    notification failure (see run_floor_worker's caller).
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        viewer_chat_ids: list[str] | None = None,
        session: requests.Session | None = None,
        sleep_fn=time.sleep,
    ):
        self._token = bot_token
        self._chat_id = chat_id  # the OWNER's chat -- kept as `_chat_id` for backward compatibility
        self._viewer_chat_ids = list(viewer_chat_ids) if viewer_chat_ids else []
        self._session = session or requests.Session()
        self._sleep = sleep_fn

    def _url(self, method: str) -> str:
        return f"{TELEGRAM_API_BASE}/bot{self._token}/{method}"

    def _send_to_chat(self, chat_id: str, data: dict, context: str = "") -> bool:
        """One sendMessage to exactly one chat. A failure here NEVER
        raises and NEVER stops the caller from trying the next recipient
        -- see _broadcast. The "bot can't initiate conversation with a
        user" 403 (Telegram's own wording) is the common real-world
        cause -- a recipient who never sent /start to the bot -- so it
        gets its own distinct log line with the actionable hint, instead
        of blending into the generic failure message. `context` (e.g.
        "signal listing_external_id=...") is prepended to the log line
        only -- never part of what gets sent.
        """
        prefix = f"{context}: " if context else ""
        try:
            self._post("sendMessage", {**data, "chat_id": chat_id})
            return True
        except TelegramError as exc:
            if "can't initiate conversation with a user" in str(exc):
                logger.error(
                    "%sfailed to deliver to chat_id=%s: recipient has not started a conversation "
                    "with the bot -- they need to send /start to the bot in Telegram (%s)",
                    prefix, chat_id, exc,
                )
            else:
                logger.error("%sfailed to deliver to chat_id=%s: %s", prefix, chat_id, exc)
            return False

    def _broadcast(self, data: dict, context: str = "") -> bool:
        """Sends `data` (everything EXCEPT chat_id) to the owner and
        every viewer. Правка: "ошибка отправки одному получателю не
        должна мешать остальным" -- every recipient is attempted
        regardless of earlier failures. Returns whether the OWNER's send
        succeeded -- that is the single boolean callers (poller.py) gate
        db.mark_alert_sent()/cooldown/retry logic on; alerts_sent stays
        one row per SIGNAL, never per recipient, per spec.
        """
        owner_ok = self._send_to_chat(self._chat_id, data, context=context)
        for viewer_chat_id in self._viewer_chat_ids:
            self._send_to_chat(viewer_chat_id, data, context=context)
        return owner_ok

    def _post(self, method: str, data: dict) -> dict:
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                resp = self._session.post(self._url(method), data=data, timeout=15)
            except requests.exceptions.RequestException as exc:
                raise TelegramError(f"{method} network error: {exc}") from exc

            if resp.status_code == 429:
                retry_after = 1.0
                try:
                    retry_after = float(resp.json().get("parameters", {}).get("retry_after", 1))
                except (ValueError, TypeError, AttributeError):
                    pass
                logger.warning("telegram 429 on %s, retry_after=%s", method, retry_after)
                self._sleep(retry_after)
                continue

            if resp.status_code >= 400:
                raise TelegramError(f"{method} failed: {resp.status_code} {resp.text[:200]}")

            try:
                return resp.json()
            except ValueError as exc:
                raise TelegramError(f"{method} returned a non-JSON body") from exc

        raise TelegramError(f"{method} exhausted {MAX_RETRY_ATTEMPTS} attempts (429)")

    def send_signal(
        self,
        signal: Signal,
        resend_after_drop: bool = False,
        test_send: bool = False,
        prefix: str | None = None,
    ) -> bool:
        """Returns True only on a confirmed successful send -- callers
        (poller.py) must gate db.mark_alert_sent() on this, never mark a
        signal sent on False (see КАК ТЕСТИРОВАТЬ item 5).

        ALWAYS sendMessage. Compact-animation experiment (a preceding
        delivery) is CLOSED, negative result -- see README "closed
        question": sendAnimation/sendSticker/sendDocument were all tried
        against the gift's own .lottie.json animation_url and confirmed
        live that Telegram rejects it as animation/sticker content and,
        as a document, delivers a raw 340KB .lottie.json FILE to the
        chat instead of any kind of card (khabibspapakha-5245.lottie.json
        -- a real user-visible defect, not a benign fallback). lottie
        renders ONLY inside Telegram's own native gift-card UI, which
        this bot cannot invoke directly. Those three methods are REMOVED
        from this codebase entirely (not merely unused) so a raw file
        can never be sent again under any condition. The link preview
        (inconsistent sizing, Telegram's call, not fixable from here --
        see README) is the only remaining approach: the t.me/nft/<tg_id>
        link is present on a VISIBLE "·" character at the end of the
        FLOOR line (see format_caption -- a zero-width anchor was tried
        and confirmed unreliable twice live, see README closed question)
        -- never on the title, so the title stays plain, non-blue text.

        `prefix`: passed straight through to format_caption -- see there
        (used only by preview_ab_test.py).
        """
        keyboard = build_keyboard(signal)
        caption = format_caption(signal, resend_after_drop=resend_after_drop, test_send=test_send, prefix=prefix)
        # prefer_small_media deliberately OMITTED: confirmed live it has
        # no effect on gift-card previews (the page's own meta tags ask
        # for a compact twitter:card=summary layout; Telegram's client
        # ignores this for NFT links regardless) -- see README, "size is
        # controlled entirely by the Telegram client" is a closed
        # question, not something worth an extra param that might
        # interfere with the unfurl actually firing at all.
        link_preview_options = {
            "is_disabled": False,
            "show_above_text": False,
        }
        return self._broadcast(
            {
                "text": caption,
                "parse_mode": "HTML",
                "link_preview_options": json.dumps(link_preview_options),
                "reply_markup": json.dumps(keyboard),
            },
            context=f"signal listing_external_id={signal.listing_external_id} observed_at={signal.observed_at}",
        )

    def send_text(self, text: str, chat_id: str | None = None) -> bool:
        """`chat_id=None` (default): broadcasts to the owner + every
        viewer -- used for the "...и ещё N сигналов" note (a signal-
        volume notice, same audience as the signals themselves).
        `chat_id=<id>`: sends ONLY to that one chat -- used by
        CommandHandler for command REPLIES, which must go back to
        whoever asked, not to everyone (see CommandHandler.poll_once /
        the "доступ ограничен" reply).
        """
        if chat_id is not None:
            return self._send_to_chat(chat_id, {"text": text, "parse_mode": "HTML"})
        return self._broadcast({"text": text, "parse_mode": "HTML"})

    def get_updates(self, offset: int | None = None, timeout: int = 0) -> list[dict]:
        """Long-poll timeout defaults to 0 (non-blocking) -- called from
        inside poller.py's main loop, which must never block waiting on
        Telegram; command responsiveness comes from being called every
        poll cycle instead (see Poller._maybe_process_commands).
        """
        params: dict = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        try:
            resp = self._post("getUpdates", params)
        except TelegramError as exc:
            logger.error("getUpdates failed: %s", exc)
            return []
        return resp.get("result", [])


_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{16,}$|^[0-9a-fA-F-]{32,}$")


def _looks_like_mrkt_token(text: str) -> bool:
    """One word, no spaces, long enough, token-shaped."""
    stripped = text.strip()
    return len(stripped.split()) == 1 and bool(_TOKEN_RE.match(stripped))


class CommandHandler:
    """Answers commands from the OWNER (all commands) and VIEWERS
    (read-only commands only: /status, /last) -- see config.py's
    TELEGRAM_OWNER_ID / TELEGRAM_VIEWER_IDS. Every other sender's
    message gets a fixed "доступ ограничен" reply and the command itself
    is never executed. A viewer sending a non-read-only command (/start,
    or anything added in the future that changes system behavior) is
    silently ignored -- same treatment as an unrecognized command, per
    spec ("любые команды, меняющие поведение системы, им недоступны"),
    NOT the "доступ ограничен" reply (that's reserved for senders who
    are neither the owner nor a viewer at all). No dialogs, no
    per-user settings: every setting comes from env (config.py), per
    spec.
    """

    READONLY_COMMANDS = {"/status", "/last", "/magazine", "/magazine_full", "/health", "/procs"}
    # Owner only: start/stop/restart one supervised process (supervisor.py).
    PROCESS_COMMANDS = {"/run", "/stop", "/restart"}
    # /magazine_reset must be sent twice by the owner within this window.
    RESET_CONFIRM_SEC = 60

    def __init__(
        self,
        notifier: TelegramNotifier,
        conn,
        owner_id: str,
        viewer_ids: list[str] | None = None,
        start_mono: float | None = None,
        journal_conn=None,
        clock=time.monotonic,
        process_control=None,
    ):
        self._notifier = notifier
        self._conn = conn
        self._owner_id = str(owner_id)
        self._viewer_ids = {str(v) for v in (viewer_ids or [])}
        self._start_mono = start_mono if start_mono is not None else time.monotonic()
        self._offset: int | None = None
        # Paper journal (journal.db); None when PAPER_JOURNAL_ENABLED is off.
        self._journal_conn = journal_conn
        self._clock = clock
        self._reset_requested_at: float | None = None
        # supervisor.Supervisor when commands run inside the supervisor;
        # None inside a poller (no process control there).
        self._process_control = process_control

    def poll_once(self) -> None:
        updates = self._notifier.get_updates(offset=self._offset, timeout=0)
        for update in updates:
            self._offset = update["update_id"] + 1
            message = update.get("message")
            if not message or "text" not in message:
                continue
            sender_id = str(message.get("from", {}).get("id", ""))
            text = message["text"].strip()

            is_owner = sender_id == self._owner_id
            is_viewer = sender_id in self._viewer_ids
            if not is_owner and not is_viewer:
                logger.warning("rejected command from unauthorized user_id=%s: %r", sender_id, text)
                self._notifier.send_text("доступ ограничен", chat_id=sender_id)
                continue

            if text.lower().startswith("/mrkt_token") or _looks_like_mrkt_token(text):
                # A bare token pasted into the chat counts as the command:
                # that is what the owner naturally does after the alert.
                if not text.lower().startswith("/mrkt_token"):
                    text = "/mrkt_token " + text
                self._cmd_mrkt_token(sender_id, is_owner, text, message.get("message_id"))
                continue
            self._dispatch(text, sender_id, is_owner=is_owner)

    def _dispatch(self, text: str, sender_id: str, is_owner: bool) -> None:
        command = text.split()[0].lower() if text else ""
        if command == "/magazine_reset":
            self._cmd_magazine_reset(sender_id, is_owner)
            return
        if not is_owner and command not in self.READONLY_COMMANDS:
            # A viewer trying a non-read-only command (or /start, or
            # anything unrecognized) -- silently ignored, same as an
            # unrecognized command from the owner. Never "доступ
            # ограничен": the viewer IS authorized, just not for this.
            return
        if command == "/start":
            self._cmd_start(sender_id)
        elif command == "/status":
            self._cmd_status(sender_id)
        elif command == "/last":
            self._cmd_last(sender_id)
        elif command == "/magazine":
            self._cmd_magazine(sender_id, full=False)
        elif command == "/magazine_full":
            self._cmd_magazine(sender_id, full=True)
        elif command == "/health":
            self._cmd_health(sender_id)
        elif command == "/procs":
            self._cmd_procs(sender_id)
        elif command in self.PROCESS_COMMANDS:
            parts = text.split()
            self._cmd_process(sender_id, command, parts[1].lower() if len(parts) > 1 else "")
        # Unrecognized commands are silently ignored -- no dialog UI, per spec.

    def _cmd_magazine(self, sender_id: str, full: bool) -> None:
        if self._journal_conn is None:
            self._notifier.send_text("журнал выключен (PAPER_JOURNAL_ENABLED)", chat_id=sender_id)
            return
        from .journal_view import magazine_text
        self._notifier.send_text(magazine_text(self._journal_conn, full=full), chat_id=sender_id)

    def _cmd_mrkt_token(self, sender_id: str, is_owner: bool, text: str, message_id) -> None:
        """Owner only: /mrkt_token <token> writes MRKT_TOKEN_FILE. Running
        processes re-read the file on the next request (no restart). The
        message with the token is deleted from the chat (best effort)."""
        if not is_owner:
            logger.warning("/mrkt_token from a non-owner chat, ignored")
            return
        # Everything after the command, however it was typed: on the next
        # line, with extra spaces, or with a @botname suffix. Measured
        # 2026-09-22: a token sent by the owner never reached the file and
        # nothing was logged, because the message did not split into
        # exactly two words.
        rest = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
        token = rest.split()[0] if rest else ""
        if len(token) < 16:
            logger.warning("/mrkt_token: no usable token in the message (%d chars after the command)", len(rest))
            self._notifier.send_text(
                "не вижу токен. Пришлите одним сообщением: /mrkt_token ТОКЕН, "
                "или просто отправьте сам токен отдельным сообщением.", chat_id=sender_id)
            return
        from .mrkt_auth import write_token
        write_token(token)
        logger.info("MRKT token updated from the bot (%d chars)", len(token))
        if message_id is not None:
            try:
                self._notifier._post("deleteMessage", {"chat_id": sender_id, "message_id": message_id})
            except Exception:
                logger.warning("could not delete the /mrkt_token message")
        self._notifier.send_text("токен MRKT сохранён, процессы подхватят его сами", chat_id=sender_id)

    def _cmd_health(self, sender_id: str) -> None:
        from html import escape
        from .health import collect, format_report
        if self._journal_conn is None:
            self._notifier.send_text("журнал выключен (PAPER_JOURNAL_ENABLED): health недоступен", chat_id=sender_id)
            return
        checks = collect(self._conn, self._journal_conn)
        self._notifier.send_text(escape(format_report(checks)), chat_id=sender_id)

    def _cmd_procs(self, sender_id: str) -> None:
        from html import escape
        if self._process_control is None:
            self._notifier.send_text("управление процессами доступно только через supervisor", chat_id=sender_id)
            return
        self._notifier.send_text(escape(self._process_control.status_text()), chat_id=sender_id)

    def _cmd_process(self, sender_id: str, command: str, name: str) -> None:
        from html import escape
        if self._process_control is None:
            self._notifier.send_text("управление процессами доступно только через supervisor", chat_id=sender_id)
            return
        action = {"/run": "start", "/stop": "stop", "/restart": "restart"}[command]
        reply = self._process_control.control(action, name)
        self._notifier.send_text(escape(reply), chat_id=sender_id)

    def _cmd_magazine_reset(self, sender_id: str, is_owner: bool) -> None:
        """Owner only, two-step: the first command asks, a second one
        within RESET_CONFIRM_SEC performs the reset."""
        if not is_owner:
            self._notifier.send_text("обнуление журнала доступно только владельцу", chat_id=sender_id)
            return
        if self._journal_conn is None:
            self._notifier.send_text("журнал выключен (PAPER_JOURNAL_ENABLED)", chat_id=sender_id)
            return
        now = self._clock()
        if self._reset_requested_at is not None and now - self._reset_requested_at <= self.RESET_CONFIRM_SEC:
            from .journal_db import reset
            reset(self._journal_conn)
            self._reset_requested_at = None
            self._notifier.send_text("журнал обнулён", chat_id=sender_id)
            return
        self._reset_requested_at = now
        self._notifier.send_text(
            "Обнулить журнал? Все сделки и баланс будут удалены.\n"
            f"Для подтверждения повторите /magazine_reset в течение {self.RESET_CONFIRM_SEC} секунд.",
            chat_id=sender_id,
        )

    def _cmd_start(self, sender_id: str) -> None:
        levels = ", ".join(sorted(config.NOTIFY_LEVELS)) or "(нет)"
        text = (
            "Gift Sniper bot\n\n"
            f"Мин. прибыль: ${config.NOTIFY_MIN_PROFIT_USD}\n"
            f"Уровни: {levels}\n"
            f"Макс. сигналов/мин: {config.NOTIFY_MAX_PER_MINUTE}"
        )
        self._notifier.send_text(text, chat_id=sender_id)

    def _cmd_status(self, sender_id: str) -> None:
        uptime_sec = int(time.monotonic() - self._start_mono)
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        count_24h = db.count_alerts_since(self._conn, since)
        last_rows = db.get_recent_alerts(self._conn, limit=1)
        last_line = "нет" if not last_rows else str(last_rows[0]["sent_at"])
        text = (
            f"Аптайм: {uptime_sec // 3600}ч {(uptime_sec % 3600) // 60}м\n"
            f"Сигналов за 24ч: {count_24h}\n"
            f"Последний сигнал: {last_line}"
        )
        self._notifier.send_text(text, chat_id=sender_id)

    def _cmd_last(self, sender_id: str) -> None:
        rows = db.get_recent_alerts(self._conn, limit=5)
        if not rows:
            self._notifier.send_text("сигналов пока не было", chat_id=sender_id)
            return
        lines = ["Последние сигналы:"]
        for r in rows:
            price = Decimal(r["new_price_nano"]) / config.NANO
            lines.append(
                f"  {r['collection_name']} #{r['gift_number']} "
                f"{r['model_name']} · {r['backdrop_name']} -- {price:.2f} ({r['sent_at']})"
            )
        self._notifier.send_text("\n".join(lines), chat_id=sender_id)
