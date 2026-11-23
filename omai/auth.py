"""Company login for the staff workspace: login + PIN → a signed token (12 h). Roles are enforced server-side."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from omai.config import settings

TTL = 12 * 3600


def hash_pin(pin: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, 200_000)
    return base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def check_pin(pin: str, stored: str | None) -> bool:
    if not stored or "$" not in stored:
        return False
    salt_b64, _ = stored.split("$", 1)
    return hmac.compare_digest(hash_pin(pin, base64.b64decode(salt_b64)), stored)


def _sign(data: bytes) -> str:
    return hmac.new(settings.session_secret.get_secret_value().encode(), data, hashlib.sha256).hexdigest()


def issue(user_id: str) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"u": user_id, "e": int(time.time()) + TTL}).encode()).decode()
    return f"{body}.{_sign(body.encode())}"


def verify(token: str) -> str | None:
    try:
        body, sig = token.rsplit(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(sig, _sign(body.encode())):
        return None
    data = json.loads(base64.urlsafe_b64decode(body))
    return data["u"] if data["e"] > time.time() else None
