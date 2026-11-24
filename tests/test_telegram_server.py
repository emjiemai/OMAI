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


def _last(s, chat):
    return s.query(Outbox).filter(Outbox.to == chat).order_by(Outbox.id.desc()).first().body


def test_greeting_buttons_status_and_retries(s):
    from omai.models import Exception_

    s.get(User, "own").telegram_chat_id, s.get(User, "otabek").telegram_chat_id = "1", "2"
    s.add(Exception_(kind="system_mismatch", ref="billz:G.A._01:FR265", detail="x"))
    s.flush()
    telegram_bot.handle_update(s, {"update_id": 900, "message": {"chat": {"id": 1}, "text": "hi"}})
    body = _last(s, "1")
    assert "Ассалому алайкум" in body["text"] and body["keyboard"][0][1]["text"] == telegram_bot.B_STATUS
    telegram_bot.handle_update(s, {"update_id": 901, "message": {"chat": {"id": 1}, "text": "check the billz"}})
    text = _last(s, "1")["text"]
    assert "OMAI ҳолати" in text and "Billz: 1 та" in text
    n = s.query(Outbox).count()
    telegram_bot.handle_update(s, {"update_id": 901, "message": {"chat": {"id": 1}, "text": "check the billz"}})
    assert s.query(Outbox).count() == n                         # Telegram's resend: answered once
    telegram_bot.handle_update(s, {"update_id": 902, "message": {"chat": {"id": 2}, "text": "/status"}})
    assert "раҳбарлар учун" in _last(s, "2")["text"]            # staff: no status
    assert all(b["text"] != telegram_bot.B_STATUS for row in _last(s, "2")["keyboard"] for b in row)


def test_stranger_gets_welcome(s):
    telegram_bot.handle_update(s, {"message": {"chat": {"id": 3}, "text": "/start"}})
    assert "/login" in _last(s, "3")["text"]
