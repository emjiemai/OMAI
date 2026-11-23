"""Telegram: linking people, task buttons, and a polling loop for testing without a public URL.

In production the API's webhook calls ``handle_update``. For testing, ``python -m omai.telegram_bot`` polls
Telegram itself (``getUpdates``) — no webhook, no public address. Use a **test bot** from @BotFather in your local
``.env`` as ``TELEGRAM_PRIMARY_BOT_TOKEN``: if the token belongs to a bot that already has a webhook (a production
bot), Telegram refuses polling with 409 and this loop stops instead of touching that bot.

People link themselves once: ``/login <login> <PIN>`` (the PIN set with ``scripts.setup --pin``).
"""

from __future__ import annotations

import time

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from omai import auth, tasks
from omai.config import WORKSPACE_URL, settings
from omai.db import init_db, session_scope
from omai.models import OPEN_TASK_STATES, Task, User
from omai.notify import deliver_outbox, task_buttons, tell

HELP = ("OMAI — warehouse tasks.\n/login &lt;login&gt; &lt;PIN&gt; — link this chat\n/tasks — my open tasks\n"
        "/logout — unlink this chat")


def _reply(s: Session, chat: str, text: str, buttons: list | None = None) -> None:
    from omai.notify import queue
    queue(s, "telegram", chat, {"text": text, "buttons": buttons or []})


def handle_update(s: Session, upd: dict) -> None:
    if "callback_query" in upd:
        return _callback(s, upd["callback_query"])
    msg = upd.get("message") or {}
    text = (msg.get("text") or "").strip()
    chat = str((msg.get("chat") or {}).get("id") or "")
    if not chat or not text:
        return
    user = s.scalar(select(User).where(User.telegram_chat_id == chat, User.active.is_(True)))
    cmd, *args = text.split()
    if cmd == "/login":
        if len(args) != 2:
            return _reply(s, chat, "Send: /login &lt;login&gt; &lt;PIN&gt;")
        u = s.scalar(select(User).where(User.login == args[0], User.active.is_(True)))
        if u is None or not auth.check_pin(args[1], u.pin_hash):
            return _reply(s, chat, "Wrong login or PIN.")
        for other in s.scalars(select(User).where(User.telegram_chat_id == chat)):
            other.telegram_chat_id = None
        u.telegram_chat_id = chat
        return _reply(s, chat, f"Linked: {u.name} ({u.role}). /tasks shows your work.")
    if user is None:
        return _reply(s, chat, HELP)
    if cmd == "/logout":
        user.telegram_chat_id = None
        return _reply(s, chat, "Unlinked.")
    if cmd == "/tasks":
        states = [st.value for st in OPEN_TASK_STATES]
        mine = s.scalars(select(Task).where(Task.assignee_id == user.id, Task.state.in_(states))
                         .order_by(Task.priority, Task.deadline)).all()
        if not mine:
            return _reply(s, chat, "No open tasks. Thank you.")
        for t in mine[:10]:
            due = t.deadline.astimezone(settings.tz).strftime("%H:%M %d.%m")
            _reply(s, chat, f"<b>{t.title}</b>\nDue {due} · {t.state}{' · overdue' if tasks.is_overdue(t) else ''}",
                   task_buttons(t.id))
        return None
    return _reply(s, chat, HELP)


def _callback(s: Session, cq: dict) -> None:
    chat = str(cq["from"]["id"])
    user = s.scalar(select(User).where(User.telegram_chat_id == chat, User.active.is_(True)))
    try:
        _, tid, action = cq["data"].split(":", 2)
    except ValueError:
        return
    task = s.get(Task, tid)
    if user is None or task is None:
        return
    if action == "submit":
        tell(s, user, f"Scan and submit in the workspace: {WORKSPACE_URL}/?task={tid}")
        return
    try:
        tasks.act(s, task, action, user)
        tell(s, user, {"start": "▶ Started", "help": "🆘 Your manager is told",
                       "problem": "⚠ Reported to your manager"}.get(action, "OK") + f": {task.title}")
    except tasks.TaskError as exc:
        tell(s, user, str(exc))


def poll() -> None:
    """Test loop: read updates, handle them, run reminders, send the outbox. Ctrl+C to stop."""
    if not settings.telegram_configured:
        raise SystemExit("Set TELEGRAM_PRIMARY_BOT_TOKEN (a test bot) in .env")
    init_db()
    base = f"https://api.telegram.org/bot{settings.telegram_primary_bot_token.get_secret_value()}"
    offset = 0
    with httpx.Client(timeout=40) as http:
        me = http.get(f"{base}/getMe").json()
        print(f"Polling as @{me.get('result', {}).get('username')} — Ctrl+C to stop", flush=True)
        while True:
            r = http.get(f"{base}/getUpdates", params={"offset": offset, "timeout": 25})
            if r.status_code == 409:
                raise SystemExit("This bot has a webhook (it is used by a server). Use a separate test bot.")
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                cq = upd.get("callback_query")
                if cq:
                    http.post(f"{base}/answerCallbackQuery", json={"callback_query_id": cq["id"]})
                with session_scope() as s:
                    handle_update(s, upd)
            with session_scope() as s:
                tasks.reminder_tick(s)
                deliver_outbox(s, http)
            time.sleep(0.5)


if __name__ == "__main__":
    poll()
