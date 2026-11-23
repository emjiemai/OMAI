"""Outgoing messages go through the outbox (AIPRO / MIYA rows stay queued for those agents to read from the
shared database; no extra webhook settings): written in the same transaction as the change that caused
them, sent by ``deliver_outbox`` with retries. A restart never loses or doubles a message."""

from __future__ import annotations

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from omai.config import settings
from omai.models import Outbox, User, now_utc

MAX_ATTEMPTS = 8


def queue(s: Session, channel: str, to: str | None, body: dict, dedupe_key: str | None = None) -> Outbox | None:
    if dedupe_key and s.scalar(select(Outbox).where(Outbox.dedupe_key == dedupe_key)):
        return None
    msg = Outbox(channel=channel, to=to, body=body, dedupe_key=dedupe_key)
    s.add(msg)
    s.flush()
    return msg


def tell(s: Session, user: User | None, text: str, buttons: list[list[dict]] | None = None,
         dedupe_key: str | None = None) -> Outbox | None:
    """A Telegram message to one person (dropped silently if they have no chat yet — the task still shows in
    their workspace)."""
    if user is None:
        return None
    return queue(s, "telegram", user.telegram_chat_id or f"user:{user.id}",
                 {"text": text, "buttons": buttons or [], "user_id": user.id}, dedupe_key)


def task_buttons(task_id: str) -> list[list[dict]]:
    return [[{"text": "▶ Start", "callback_data": f"t:{task_id}:start"},
             {"text": "🆘 Need help", "callback_data": f"t:{task_id}:help"}],
            [{"text": "⚠ Report problem", "callback_data": f"t:{task_id}:problem"},
             {"text": "✅ Submit", "callback_data": f"t:{task_id}:submit"}]]


def _send(client: httpx.Client, m: Outbox) -> None:
    if settings.dry_run:
        raise _Skip()
    if m.channel == "telegram":
        if not settings.telegram_configured or not m.to or m.to.startswith("user:"):
            raise _Skip()  # no channel: the workspace shows the task anyway
        payload: dict = {"chat_id": m.to, "text": m.body["text"], "parse_mode": "HTML"}
        if m.body.get("buttons"):
            payload["reply_markup"] = {"inline_keyboard": m.body["buttons"]}
        r = client.post(f"https://api.telegram.org/bot{settings.telegram_primary_bot_token.get_secret_value()}/sendMessage",
                        json=payload)
        r.raise_for_status()
    else:
        raise ValueError(f"unknown channel {m.channel}")


class _Skip(Exception):
    pass


def deliver_outbox(s: Session, client: httpx.Client | None = None, limit: int = 100) -> int:
    """Send what is queued; returns how many were sent. A message with no channel configured is skipped."""
    sent = 0
    rows = s.scalars(select(Outbox).where(Outbox.status == "queued", Outbox.channel == "telegram").order_by(Outbox.created_at).limit(limit)).all()
    own = client is None
    client = client or httpx.Client(timeout=20)
    try:
        for m in rows:
            try:
                _send(client, m)
                m.status, m.sent_at = "sent", now_utc()
                sent += 1
            except _Skip:
                m.status = "skipped"
            except Exception as exc:  # noqa: BLE001
                m.attempts += 1
                m.last_error = str(exc)[:500]
                if m.attempts >= MAX_ATTEMPTS:
                    m.status = "failed"
    finally:
        if own:
            client.close()
    return sent
