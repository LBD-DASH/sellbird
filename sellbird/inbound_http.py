"""Public webhook for Resend inbound mail.

The dashboard stays on localhost. This app is separate so a reverse proxy
can expose one path. The signature is checked before anything is stored.
"""

import json
import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from sellbird.integrations.mail_provider import looks_like_bounce
from sellbird.integrations.resend_mail import parse_address
from sellbird.resend_sign import verify_svix

logger = logging.getLogger("sellbird.inbound")


def create_app(state_factory=None):
    app = FastAPI(title="Sellbird Resend inbound", docs_url=None, redoc_url=None, openapi_url=None)

    def _state():
        if state_factory is not None:
            return state_factory()
        from sellbird.state import StateManager
        return StateManager()

    @app.post("/resend/inbound")
    async def resend_inbound(request: Request):
        body = await request.body()
        secret = (os.getenv("RESEND_WEBHOOK_SECRET") or "").strip()
        headers = {k.lower(): v for k, v in request.headers.items()}
        if not verify_svix(body, headers, secret):
            logger.error("Resend webhook rejected: signature did not verify.")
            return JSONResponse({"error": "bad signature"}, status_code=401)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return JSONResponse({"error": "body is not JSON"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "body is not an object"}, status_code=400)
        event = str(payload.get("type") or "")
        if event and event != "email.received":
            return {"ok": True, "ignored": event}
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        provider_id = str(data.get("email_id") or data.get("id") or "")
        sender = parse_address(str(data.get("from") or ""))
        if not provider_id or not sender:
            return JSONResponse({"error": "missing email id or from address"}, status_code=400)
        subject = str(data.get("subject") or "")
        text = str(data.get("text") or data.get("body") or "")
        state = _state()
        await state.init_db()
        await state.add_inbound_mail(
            provider_id=provider_id,
            from_email=sender,
            subject=subject,
            body=text,
            message_id=str(data.get("message_id") or ""),
            in_reply_to=str(data.get("in_reply_to") or ""),
            is_bounce=looks_like_bounce(sender, subject),
        )
        logger.info("Stored Resend inbound %s.", provider_id)
        return {"ok": True}

    return app
