"""Svix signature check for Resend inbound webhooks.

Resend signs the raw body. A missing secret, a stale timestamp, or a
signature that does not match is rejected. The check never logs the secret.
"""

import base64
import hashlib
import hmac
import time


def verify_svix(body: bytes, headers: dict, secret: str, *, now: float | None = None,
                tolerance: int = 300) -> bool:
    """True when ``body`` was signed with ``secret`` (a whsec_ key or raw base64)."""
    if not secret or body is None:
        return False
    wanted = {k.lower(): v for k, v in (headers or {}).items()}
    msg_id = _one(wanted.get("svix-id") or wanted.get("webhook-id"))
    timestamp = _one(wanted.get("svix-timestamp") or wanted.get("webhook-timestamp"))
    signature = _one(wanted.get("svix-signature") or wanted.get("webhook-signature"))
    if not (msg_id and timestamp and signature):
        return False
    try:
        ts = int(str(timestamp).strip())
    except ValueError:
        return False
    current = time.time() if now is None else now
    if abs(current - ts) > tolerance:
        return False
    key = _secret_bytes(secret)
    if not key:
        return False
    signed = f"{msg_id}.{timestamp}.".encode() + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    for part in str(signature).split():
        version, sep, sig = part.partition(",")
        if sep and version == "v1" and hmac.compare_digest(sig, expected):
            return True
    return False


def _one(value) -> str:
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else ""
    return str(value or "")


def _secret_bytes(secret: str) -> bytes:
    raw = secret.strip()
    if raw.startswith("whsec_"):
        raw = raw[len("whsec_"):]
    try:
        return base64.b64decode(raw, validate=True)
    except Exception:
        return b""
