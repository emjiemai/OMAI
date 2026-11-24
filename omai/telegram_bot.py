"""Telegram: linking people, task buttons, and a polling loop for testing without a public URL.

In production the API's webhook calls ``handle_update``. For testing, ``python -m omai.telegram_bot`` polls
Telegram itself (``getUpdates``) — no webhook, no public address. Use a **test bot** from @BotFather in your local
``.env`` as ``OMAI_TELEGRAM_BOT_TOKEN``: if the token belongs to a bot that already has a webhook (a production
bot), Telegram refuses polling with 409 and this loop stops instead of touching that bot.

People link themselves once: ``/login <login> <PIN>`` (the PIN set with ``scripts.setup --pin``).
"""

from __future__ import annotations

import time
from collections import deque

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from omai import auth, tasks
from omai.config import settings
from omai.db import init_db, session_scope
from omai.models import OPEN_TASK_STATES, ROLE_UZ, Role, Task, User
from omai.notify import deliver_outbox, task_buttons, tell

WELCOME = ("Ассалому алайкум! Бу OMAI — омбор ёрдамчиси.\nУланиш учун юборинг: /login &lt;логин&gt; &lt;PIN&gt;\n"
           "Логин ва PIN'ни раҳбарингиз ёки IT беради.")
B_TASKS, B_STATUS, B_HELP = "📋 Топшириқларим", "📊 Ҳолат", "❓ Ёрдам"
STATUS_ROLES = {Role.manager, Role.accountant, Role.owner, Role.it}
SOURCE_UZ = {"sap": "SAP", "1c": "1С", "billz": "Billz"}
_seen: deque[int] = deque(maxlen=1000)   # Telegram resends an update it thinks failed: answer it once


def _keyboard(user: User) -> list[list[dict]]:
    first = [{"text": B_TASKS}] + ([{"text": B_STATUS}] if Role(user.role) in STATUS_ROLES else [])
    return [first, [{"text": B_HELP}]]


def _reply(s: Session, chat: str, text: str, buttons: list | None = None, keyboard: list | None = None,
           remove_keyboard: bool = False) -> None:
    from omai.notify import queue
    body: dict = {"text": text, "buttons": buttons or []}
    if keyboard:
        body["keyboard"] = keyboard
    if remove_keyboard:
        body["remove_keyboard"] = True
    queue(s, "telegram", chat, body)


def _help(user: User) -> str:
    lines = [f"{user.name}, OMAI сизга топшириқларни юборади ва эслатади.",
             f"{B_TASKS} — очиқ топшириқларингиз, ҳар бирида тугмалар: Бошлаш, Ёрдам керак, Муаммо, Топшириш."]
    if Role(user.role) in STATUS_ROLES:
        lines.append(f"{B_STATUS} — SAP, 1С, Billz янгилиги ва улар орасидаги фарқлар сони.")
    lines.append("/logout — чатни узиш.")
    return "\n".join(lines)


def status_text(s: Session) -> str:
    """Counts and freshness only — no quantities, no money (IT sees no company figures)."""
    from omai.insights import BILLZ_SHOP_SAP_WAREHOUSES
    from omai.models import Exception_, Location
    from omai.sources import freshness

    out = ["📊 <b>OMAI ҳолати</b>", "", "<b>Манбалар</b>"]
    for src in freshness(s):
        name = SOURCE_UZ.get(src["source"], src["source"])
        if src["last_success"] is None:
            out.append(f"⚪ {name} — уланмаган")
        else:
            out.append(f"{'🔴' if src['stale'] else '✅'} {name} — {src['age_minutes']:.0f} дақ. олдин"
                       + (" (эскирган)" if src["stale"] else ""))
    mism = s.scalars(select(Exception_).where(Exception_.kind == "system_mismatch",
                                              Exception_.status == "open")).all()
    by = {k: sum(1 for e in mism if (e.ref or "").startswith(k + ":")) for k in SOURCE_UZ}
    out += ["", "<b>Тизимлар фарқи</b> (очиқ, товар × омбор)"]
    for k, name in SOURCE_UZ.items():
        out.append(f"• {name}: {by[k]} та")
    shop = s.scalar(select(Location).where(Location.billz_shop_id.is_not(None)))
    if shop is not None:
        out.append(f"Billz дўкони {shop.code} SAP'нинг {', '.join(BILLZ_SHOP_SAP_WAREHOUSES.get(shop.code, (shop.code,)))} "
                   "омборлари йиғиндиси билан солиштирилади.")
    else:
        out.append("Billz дўкони ҳали омборга уланмаган.")
    other = s.scalar(select(func.count()).select_from(Exception_).where(Exception_.status == "open",
                                                                         Exception_.kind != "system_mismatch"))
    states = [st.value for st in OPEN_TASK_STATES]
    overdue = sum(1 for t in s.scalars(select(Task).where(Task.state.in_(states))) if tasks.is_overdue(t))
    pilot = [loc.code for loc in s.scalars(select(Location).where(Location.movement_source == "omai",
                                                                  Location.active.is_(True)))]
    out += ["", f"Бошқа очиқ муаммолар: {other} та", f"Муддати ўтган топшириқлар: {overdue} та",
            f"OMAI юритаётган омборлар: {', '.join(pilot) or 'ҳали йўқ'}",
            "", f"Батафсил: {settings.public_url}/owner"]
    return "\n".join(out)


def _tasks(s: Session, chat: str, user: User) -> None:
    states = [st.value for st in OPEN_TASK_STATES]
    mine = s.scalars(select(Task).where(Task.assignee_id == user.id, Task.state.in_(states))
                     .order_by(Task.priority, Task.deadline)).all()
    if not mine:
        return _reply(s, chat, "Очиқ топшириқ йўқ. Раҳмат.", keyboard=_keyboard(user))
    for t in mine[:10]:
        due = t.deadline.astimezone(settings.tz).strftime("%H:%M %d.%m")
        _reply(s, chat, f"<b>{t.title}</b>\nМуддат {due} · {tasks.STATE_UZ.get(t.state, t.state)}"
                        f"{' · муддати ўтган' if tasks.is_overdue(t) else ''}", task_buttons(t.id))
    return None


def handle_update(s: Session, upd: dict) -> None:
    uid = upd.get("update_id")
    if isinstance(uid, int):
        if uid in _seen:
            return None
        _seen.append(uid)
    if "callback_query" in upd:
        return _callback(s, upd["callback_query"])
    msg = upd.get("message") or {}
    text = (msg.get("text") or "").strip()
    chat = str((msg.get("chat") or {}).get("id") or "")
    if not chat or not text:
        return None
    user = s.scalar(select(User).where(User.telegram_chat_id == chat, User.active.is_(True)))
    cmd, *args = text.split()
    cmd = cmd.split("@", 1)[0].lower()
    if cmd == "/login":
        if len(args) != 2:
            return _reply(s, chat, "Илтимос, шундай юборинг: /login &lt;логин&gt; &lt;PIN&gt;")
        u = s.scalar(select(User).where(User.login == args[0].lower(), User.active.is_(True)))
        if u is None or not auth.check_pin(args[1], u.pin_hash):
            return _reply(s, chat, "Логин ёки PIN нотўғри.")
        for other in s.scalars(select(User).where(User.telegram_chat_id == chat)):
            other.telegram_chat_id = None
        u.telegram_chat_id = chat
        return _reply(s, chat, f"Уланди: {u.name} ({ROLE_UZ.get(u.role, u.role)}). Пастдаги тугмалардан фойдаланинг.",
                      keyboard=_keyboard(u))
    if user is None:
        return _reply(s, chat, WELCOME, remove_keyboard=True)
    if cmd == "/logout":
        user.telegram_chat_id = None
        return _reply(s, chat, "Чат узилди. Қайта уланиш: /login &lt;логин&gt; &lt;PIN&gt;", remove_keyboard=True)
    if cmd == "/tasks" or text == B_TASKS:
        return _tasks(s, chat, user)
    low = text.lower()
    if cmd == "/status" or text == B_STATUS or any(w in low for w in ("billz", "sap", "1c", "1с", "ҳолат", "status")):
        if Role(user.role) not in STATUS_ROLES:
            return _reply(s, chat, "Бу маълумот раҳбарлар учун. Топшириқларингиз — " + B_TASKS,
                          keyboard=_keyboard(user))
        return _reply(s, chat, status_text(s), keyboard=_keyboard(user))
    if cmd in ("/start", "/help") or text == B_HELP:
        return _reply(s, chat, _help(user), keyboard=_keyboard(user))
    return _reply(s, chat, f"Ассалому алайкум, {user.name}! Мен ҳозирча саволларга жавоб бермайман — "
                           "пастдаги тугмалардан бирини танланг.", keyboard=_keyboard(user))


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
        tell(s, user, f"Иш саҳифасида сканерлаб топширинг: {settings.public_url}/?task={tid}")
        return
    try:
        tasks.act(s, task, action, user)
        tell(s, user, {"start": "▶ Бошланди", "help": "🆘 Менежерингизга айтилди",
                       "problem": "⚠ Менежерингизга хабар берилди"}.get(action, "Бажарилди") + f": {task.title}")
    except tasks.TaskError as exc:
        tell(s, user, str(exc))


COMMANDS = [{"command": "login", "description": "Чатни улаш: /login логин PIN"},
            {"command": "tasks", "description": "Очиқ топшириқларим"},
            {"command": "status", "description": "SAP, 1С, Billz ҳолати (раҳбарлар учун)"},
            {"command": "logout", "description": "Чатни узиш"}]


def register_webhook() -> str:
    """Point OMAI's own bot at this server (production only). Returns a short status for the log — never the token."""
    if settings.telegram_token_is_company_bot:
        return "refused: company bot token"
    if not settings.telegram_configured:
        return "skipped: OMAI_TELEGRAM_BOT_TOKEN not set"
    if not settings.public_url.startswith("https://"):
        return "skipped: no public https address"
    base = f"https://api.telegram.org/bot{settings.omai_telegram_bot_token.get_secret_value()}"
    url = f"{settings.public_url}/telegram/webhook/{settings.derived_secret('telegram')}"
    try:
        with httpx.Client(timeout=15) as http:
            r = http.post(f"{base}/setWebhook", json={"url": url, "allowed_updates": ["message", "callback_query"]})
            http.post(f"{base}/setMyCommands", json={"commands": COMMANDS})
        return "webhook set" if r.status_code == 200 and r.json().get("ok") else f"webhook failed: HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        return f"webhook failed: {type(exc).__name__}"


def poll() -> None:
    """Test loop: read updates, handle them, run reminders, send the outbox. Ctrl+C to stop."""
    if settings.telegram_token_is_company_bot:
        raise SystemExit("OMAI_TELEGRAM_BOT_TOKEN is a company bot's token (Admin / OPS Manager) — use OMAI's own bot")
    if not settings.telegram_configured:
        raise SystemExit("Set OMAI_TELEGRAM_BOT_TOKEN (a test bot) in .env")
    init_db()
    base = f"https://api.telegram.org/bot{settings.omai_telegram_bot_token.get_secret_value()}"
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
