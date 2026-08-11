"""Email notifications.

Uses stdlib `smtplib`, so the app still installs nothing. Two rules govern
everything here:

1. **A notification must never break the thing it is reporting on.** Sending
   happens on a background thread and every failure is swallowed after logging.
   A dead SMTP server must not stop someone submitting a result.
2. **Nothing is sent that the player didn't ask for.** Each of the four
   notification types is a separate opt-in, and every message carries a
   one-click unsubscribe link for its own type that needs no login.

If SMTP isn't configured the whole module is inert -- `send` returns
immediately and the settings page says so.
"""

from __future__ import annotations

import hmac
import queue
import smtplib
import threading
from email.message import EmailMessage
from hashlib import sha256
from typing import List, Optional, Sequence

from . import divisions as div
from .config import Config
from .storage import Database, Match, Player, Season

# Notification kinds, matching the players.notify_* columns.
CONFIRM = "confirm"
RESULT = "result"
WEEKLY = "weekly"
SEASON = "season"

KIND_LABELS = {
    CONFIRM: "A result needs my confirmation",
    RESULT: "My submitted result was confirmed or disputed",
    WEEKLY: "Weekly ladder summary",
    SEASON: "New season started",
}

KIND_HINTS = {
    CONFIRM: "The one that keeps the ladder moving -- without it, results sit "
             "unconfirmed until someone happens to check the site.",
    RESULT: "Closes the loop after you submit: your rating moved, or there's a "
            "disagreement to sort out.",
    WEEKLY: "A digest of the week's results and where you stand.",
    SEASON: "Your carried-over starting rating when a new season begins.",
}


def unsubscribe_token(secret: str, player_id: int, kind: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), f"{player_id}:{kind}".encode("utf-8"), sha256
    ).hexdigest()[:32]


def verify_unsubscribe(secret: str, player_id: int, kind: str, token: str) -> bool:
    return hmac.compare_digest(unsubscribe_token(secret, player_id, kind), token or "")


class Mailer:
    """Queues and sends notification emails on a background thread."""

    def __init__(self, config: Config, db: Database, *, sender=None):
        self.config = config
        self.db = db
        # Injectable transport so tests never open a socket.
        self._sender = sender or self._smtp_send
        self._queue: "queue.Queue[Optional[EmailMessage]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.sent: List[EmailMessage] = []      # inspectable in tests
        self.failures = 0

    # ------------------------------------------------------------- lifecycle
    def _ensure_worker(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._worker, name="mailer", daemon=True)
            self._thread.start()

    def _worker(self) -> None:
        while True:
            message = self._queue.get()
            if message is None:
                self._queue.task_done()
                return
            try:
                self._sender(message)
                self.sent.append(message)
            except Exception as exc:            # noqa: BLE001
                self.failures += 1
                print(f"  ! email to {message['To']} failed: {exc}", flush=True)
            finally:
                self._queue.task_done()

    def flush(self, timeout: float = 5.0) -> None:
        """Wait for the queue to drain. Used by tests and by the CLI."""
        if self._thread:
            self._queue.join()

    def _smtp_send(self, message: EmailMessage) -> None:
        cfg = self.config
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15) as smtp:
            if cfg.smtp_starttls:
                smtp.starttls()
            if cfg.smtp_user:
                smtp.login(cfg.smtp_user, cfg.smtp_password)
            smtp.send_message(message)

    # ---------------------------------------------------------------- public
    def send(self, event: str, **context) -> None:
        """Entry point wired into LadderService as its notifier."""
        if not self.config.email_enabled:
            return
        handler = {
            "confirm_needed": self._confirm_needed,
            "result_confirmed": self._result_settled,
            "result_disputed": self._result_settled,
            "season_started": self._season_started,
        }.get(event)
        if handler:
            handler(event=event, **context)

    def send_test(self, to_address: str) -> str:
        """Send one email right now and report what happened, in words.

        Deliberately synchronous and deliberately not swallowing errors: the
        whole point is to see the SMTP server's actual complaint while setting
        this up, rather than watching nothing arrive and having to guess.
        """
        if not self.config.email_enabled:
            return ("Email isn't configured yet -- set smtp_host and smtp_from "
                    "in config.json.")
        to_address = (to_address or "").strip()
        if "@" not in to_address:
            return "That doesn't look like an email address."

        message = EmailMessage()
        message["Subject"] = f"[{self.config.club_name}] Test email"
        message["From"] = self.config.smtp_from
        message["To"] = to_address
        message.set_content(
            "This is a test from your tennis ladder.\n\n"
            "If you're reading it, email is working: players who opt in will "
            "get notifications about results and matches.\n"
        )
        try:
            self._sender(message)
        except Exception as exc:                 # noqa: BLE001
            return f"Failed: {type(exc).__name__}: {exc}"
        self.sent.append(message)
        return f"Sent to {to_address}. If it doesn't arrive, check spam."

    def send_weekly_summary(self, lines: Sequence[str], subject: str) -> int:
        """Broadcast the weekly digest to everyone opted in. Returns count."""
        if not self.config.email_enabled:
            return 0
        body = "\n".join(lines)
        count = 0
        for player in self.db.list_players(active_only=True):
            if not player.wants(WEEKLY):
                continue
            self._queue_message(player, WEEKLY, subject, body)
            count += 1
        return count

    # ---------------------------------------------------------------- events
    def _confirm_needed(self, *, match: Match, event: str = "") -> None:
        names = self._names()
        submitter_side = self._submitter_side(match)
        for player_id in match.players:
            if player_id in submitter_side:
                continue
            player = self.db.get_player(player_id)
            if not player or not player.wants(CONFIRM):
                continue
            self._queue_message(
                player, CONFIRM,
                f"Confirm your {div.get(match.division).label} result",
                self._match_body(
                    match, names,
                    lead=f"{names.get(match.submitted_by, 'Someone')} submitted a "
                         f"result with you in it. It won't count towards the "
                         f"ladder until you confirm it.",
                    action="Confirm or dispute it here:",
                    path="/pending",
                ),
            )

    def _result_settled(self, *, match: Match, event: str,
                        actor_id: Optional[int] = None) -> None:
        names = self._names()
        disputed = event == "result_disputed"
        for player_id in self._submitter_side(match):
            player = self.db.get_player(player_id)
            if not player or not player.wants(RESULT):
                continue
            actor = names.get(actor_id, "An admin")
            lead = (f"{actor} disputed the result you submitted. It won't count "
                    f"until it's sorted out and re-entered."
                    if disputed else
                    f"{actor} confirmed the result you submitted. Ratings have "
                    f"been updated.")
            self._queue_message(
                player, RESULT,
                ("Result disputed" if disputed else "Result confirmed"),
                self._match_body(match, names, lead=lead,
                                 action="See where you stand:",
                                 path=f"/player/{player_id}"),
            )

    def _season_started(self, *, season: Season, event: str = "") -> None:
        for player in self.db.list_players(active_only=True):
            if not player.wants(SEASON):
                continue
            body = (
                f"{season.name} has started.\n\n"
                "Your rating carries over from last season, with its uncertainty\n"
                "widened a little -- so where you finished seeds where you start,\n"
                "without locking it in.\n"
            )
            self._queue_message(
                player, SEASON, f"{season.name} has started",
                body + self._link("See the ladders:", "/"),
            )

    # --------------------------------------------------------------- helpers
    def _names(self) -> dict:
        return {p.id: p.name for p in self.db.list_players()}

    @staticmethod
    def _submitter_side(match: Match) -> List[int]:
        if match.submitted_by is None:
            return []
        side = match.side_of(match.submitted_by)
        if side is None:
            return [match.submitted_by]
        return match.side_a if side == "a" else match.side_b

    def _match_body(self, match: Match, names: dict, *, lead: str,
                    action: str, path: str) -> str:
        winners = " & ".join(names.get(p, "?") for p in match.winners)
        losers = " & ".join(names.get(p, "?") for p in match.losers)
        return (
            f"{lead}\n\n"
            f"  {div.get(match.division).label}, {match.played_on}\n"
            f"  {winners} def. {losers}\n"
            f"  {match.score}\n"
            + (f"  Note: {match.note}\n" if match.note else "")
            + self._link(action, path)
        )

    def _link(self, action: str, path: str) -> str:
        base = self.config.base_url.rstrip("/")
        if not base:
            return f"\n{action} open the ladder site.\n"
        return f"\n{action}\n  {base}{path}\n"

    def _queue_message(self, player: Player, kind: str,
                       subject: str, body: str) -> None:
        message = EmailMessage()
        message["Subject"] = f"[{self.config.club_name}] {subject}"
        message["From"] = self.config.smtp_from
        message["To"] = player.email

        base = self.config.base_url.rstrip("/")
        token = unsubscribe_token(self.config.secret_key, player.id, kind)
        unsub = (f"{base}/unsubscribe?p={player.id}&k={kind}&t={token}"
                 if base else "your settings page on the ladder site")
        if base:
            # Lets mail clients offer one-click unsubscribe themselves.
            message["List-Unsubscribe"] = f"<{unsub}>"

        message.set_content(
            f"{body}\n"
            f"--\n"
            f"You're getting this because \"{KIND_LABELS[kind]}\" is on in your\n"
            f"ladder settings. Turn just this one off:\n  {unsub}\n"
        )
        self._ensure_worker()
        self._queue.put(message)
