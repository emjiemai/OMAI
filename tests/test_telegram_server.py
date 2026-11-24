from __future__ import annotations

import httpx

from omai import telegram_bot
from omai.auth import hash_pin
from omai.config import settings
from omai.models import Outbox, User


def test_login_reply_is_queued_and_linked(s):
    s.get(User, "otabek").pin_hash = hash_pin("1111")
    telegram_bot.handle_update(s, {"message": {"chat": {"id": 77}, "text": "/login otabek 1111"}})
    assert s.get(User, "otabek").telegram_chat_id == "77"
    assert s.query(Outbox).filter(Outbox.to == "77").count() == 1


def test_register_webhook_uses_render_url(monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://omai-x.onrender.com")
    monkeypatch.setattr(settings, "omai_telegram_bot_token", type(settings.omai_telegram_bot_token)("123:abc"))
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen[req.url.path.rsplit("/", 1)[-1]] = req.content
        return httpx.Response(200, json={"ok": True})

    real = httpx.Client
    monkeypatch.setattr(telegram_bot.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    assert telegram_bot.register_webhook() == "webhook set"
    assert b"https://omai-x.onrender.com/telegram/webhook/" in seen["setWebhook"]
    assert "setMyCommands" in seen


def test_register_webhook_skips_without_public_url(monkeypatch):
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    monkeypatch.setattr(settings, "omai_telegram_bot_token", type(settings.omai_telegram_bot_token)("123:abc"))
    assert telegram_bot.register_webhook().startswith("skipped")


def _say(s, chat, text, update_id=None):
    """Send one update; return the reply bodies it queued (outbox ids aren't ordered, so compare before / after)."""
    before = {o.id for o in s.query(Outbox)}
    upd = {"message": {"chat": {"id": int(chat)}, "text": text}}
    if update_id is not None:
        upd["update_id"] = update_id
    telegram_bot.handle_update(s, upd)
    return [o.body for o in s.query(Outbox) if o.id not in before and o.to == chat]


def test_greeting_buttons_status_and_retries(s):
    from omai.models import Exception_

    s.get(User, "own").telegram_chat_id, s.get(User, "otabek").telegram_chat_id = "1", "2"
    s.add(Exception_(kind="system_mismatch", ref="billz:G.A._01:FR265", detail="x"))
    s.flush()
    (body,) = _say(s, "1", "hi", 900)
    assert "Ассалому алайкум" in body["text"] and body["keyboard"][0][1]["text"] == telegram_bot.B_STATUS
    (body,) = _say(s, "1", "check the billz", 901)
    assert "OMAI ҳолати" in body["text"] and "Billz: 1 та" in body["text"]
    assert _say(s, "1", "check the billz", 901) == []                # Telegram's resend: answered once
    (body,) = _say(s, "2", "/status", 902)
    assert "раҳбарлар учун" in body["text"]                          # staff: no status
    assert all(b["text"] != telegram_bot.B_STATUS for row in body["keyboard"] for b in row)


def test_stranger_gets_welcome(s):
    (body,) = _say(s, "3", "/start")
    assert "/login" in body["text"]
