"""Resend mail provider. Sends only through the Resend API.

Every message gets a stable Idempotency-Key, the configured postal footer,
and an unsubscribe line. Replies are read from the receiving API, and from
rows a verified webhook stored. A failed call is logged and returned. It is
not retried against another sender.
"""

import logging
import os
import re
from collections.abc import Sequence

import httpx

from sellbird.config import compliance_footer_lines
from sellbird.integrations.mail_provider import (
    InboundMessage, MailProvider, SendResult, looks_like_bounce,
)

logger = logging.getLogger("sellbird.resend")

API = "https://api.resend.com"
_EMAIL = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)


def parse_address(value: str) -> str:
    """The email address inside ``value`` (bare, or 'Name <addr>')."""
    match = _EMAIL.search(value or "")
    return match.group(0).strip().lower() if match else ""


def from_header(config) -> str:
    persona = getattr(config, "persona", None)
    address = (getattr(persona, "email", "") or "").strip()
    name = (getattr(persona, "name", "") or "").strip()
    if name and address:
        return f"{name} <{address}>"
    return address


class ResendProvider(MailProvider):
    name = "resend"
    carries_headers = True

    def __init__(self, config, env, state=None, transport=None):
        self.config = config
        self.env = env
        self.state = state
        self.transport = transport

    def bind_state(self, state) -> None:
        self.state = state

    def _api_key(self) -> str:
        key = getattr(self.env, "resend_api_key", "") or os.getenv("RESEND_API_KEY", "")
        return str(key or "").strip()

    def is_configured(self) -> bool:
        return bool(self._api_key()) and bool(from_header(self.config))

    def _auth(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key()}",
            "Content-Type": "application/json",
        }

    def _with_footer(self, body: str) -> str:
        footer = "\n".join(compliance_footer_lines(self.config))
        text = (body or "").rstrip()
        if not footer or text.endswith(footer):
            return text
        return text + "\n\n" + footer

    async def send_email(
        self,
        to_email: str,
        subject: str,
        body: str,
        thread_ref: str = "",
        in_reply_to: str = "",
        headers: Sequence[tuple[str, str]] = (),
        idempotency_key: str = "",
    ) -> SendResult:
        sender = from_header(self.config)
        if not self._api_key():
            logger.error("Resend send not attempted: RESEND_API_KEY is empty.")
            return SendResult(ok=False, error="RESEND_API_KEY is empty")
        if not sender or "@" not in sender:
            logger.error("Resend send not attempted: the from address is empty.")
            return SendResult(ok=False, error="from address is empty")
        text = self._with_footer(body)
        extra = {name: value for name, value in headers}
        reply_to = parse_address(sender)
        if reply_to and "List-Unsubscribe" not in extra:
            extra["List-Unsubscribe"] = f"<mailto:{reply_to}?subject=unsubscribe>"
        payload = {
            "from": sender,
            "to": [to_email],
            "subject": subject or "",
            "text": text,
        }
        if extra:
            payload["headers"] = extra
        if in_reply_to:
            payload["headers"] = {**payload.get("headers", {}), "In-Reply-To": in_reply_to,
                                  "References": in_reply_to}
        key = (idempotency_key or "").strip() or f"sellbird-{to_email}-{subject}"
        req_headers = {**self._auth(), "Idempotency-Key": key[:256]}
        try:
            response = await self._request("POST", f"{API}/emails", json=payload, headers=req_headers)
        except httpx.HTTPError as e:
            logger.error("Resend send failed (%s). Nothing else was tried.", type(e).__name__)
            return SendResult(ok=False, error=f"resend request failed ({type(e).__name__})",
                              uncertain=True)
        if response.status_code in (200, 201):
            data = _json(response)
            message_id = str((data or {}).get("id") or "")
            return SendResult(ok=True, message_id=message_id, thread_ref=thread_ref or message_id)
        if response.status_code in (429, 500, 502, 503, 504):
            # The request may have been accepted. The same idempotency key is
            # the only safe retry, and the sender does not invent a new one.
            logger.error("Resend returned HTTP %s. Not sending by another route.",
                         response.status_code)
            return SendResult(ok=False, error=f"resend HTTP {response.status_code}", uncertain=True)
        logger.error("Resend rejected the message with HTTP %s.", response.status_code)
        return SendResult(ok=False, error=f"resend HTTP {response.status_code}")

    async def get_replies(self, limit: int = 20) -> list[InboundMessage]:
        found: list[InboundMessage] = []
        seen: set[str] = set()
        if self.state is not None:
            try:
                for row in await self.state.list_inbound_mail(limit=limit):
                    msg = _row_message(row)
                    if msg.provider_id and msg.provider_id not in seen:
                        seen.add(msg.provider_id)
                        found.append(msg)
            except Exception as e:
                logger.warning("Could not read stored inbound mail (%s).", type(e).__name__)
        if not self._api_key():
            return found
        try:
            response = await self._request(
                "GET", f"{API}/emails/receiving",
                params={"limit": max(1, min(int(limit), 100))},
                headers=self._auth(),
            )
        except httpx.HTTPError as e:
            logger.error("Resend inbound poll failed (%s).", type(e).__name__)
            return found
        if response.status_code == 404:
            logger.info("Resend receiving is not enabled on this account. Webhook rows still apply.")
            return found
        if response.status_code != 200:
            logger.error("Resend inbound poll returned HTTP %s.", response.status_code)
            return found
        data = _json(response) or {}
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return found
        for item in rows:
            if not isinstance(item, dict):
                continue
            message = await self._message_from_api(item)
            if message is None or message.provider_id in seen:
                continue
            seen.add(message.provider_id)
            found.append(message)
            if self.state is not None:
                try:
                    await self.state.add_inbound_mail(
                        provider_id=message.provider_id,
                        from_email=message.from_email,
                        subject=message.subject,
                        body=message.body,
                        message_id=message.message_id,
                        in_reply_to=message.in_reply_to,
                        is_bounce=message.is_bounce,
                    )
                except Exception as e:
                    logger.warning("Could not store a polled reply (%s).", type(e).__name__)
        return found[:limit]

    async def _message_from_api(self, item: dict) -> InboundMessage | None:
        provider_id = str(item.get("id") or item.get("email_id") or "")
        if not provider_id:
            return None
        body = str(item.get("text") or item.get("body") or "")
        if not body:
            try:
                detail = await self._request(
                    "GET", f"{API}/emails/receiving/{provider_id}", headers=self._auth())
            except httpx.HTTPError as e:
                logger.warning("Resend could not fetch %s (%s).", provider_id, type(e).__name__)
                detail = None
            if detail is not None and detail.status_code == 200:
                payload = _json(detail) or {}
                body = str(payload.get("text") or payload.get("html") or "")
                item = {**item, **payload}
        sender = parse_address(str(item.get("from") or ""))
        subject = str(item.get("subject") or "")
        headers = item.get("headers") if isinstance(item.get("headers"), dict) else {}
        message_id = str(item.get("message_id") or headers.get("message-id") or "")
        in_reply = str(headers.get("in-reply-to") or headers.get("In-Reply-To") or "")
        return InboundMessage(
            provider_id=provider_id,
            from_email=sender,
            subject=subject,
            body=body,
            message_id=message_id,
            in_reply_to=in_reply,
            is_bounce=looks_like_bounce(sender, subject),
        )

    async def test_connection(self) -> tuple[bool, str]:
        if not self._api_key():
            return False, "RESEND_API_KEY is empty"
        try:
            response = await self._request("GET", f"{API}/domains", headers=self._auth())
        except httpx.HTTPError as e:
            return False, f"Resend could not be reached ({type(e).__name__})"
        if response.status_code == 200:
            return True, "Resend accepted the API key"
        return False, f"Resend returned HTTP {response.status_code}"

    async def _request(self, method: str, url: str, **kwargs):
        if self.transport is not None:
            return await self.transport(method, url, **kwargs)
        async with httpx.AsyncClient(timeout=30.0) as client:
            return await client.request(method, url, **kwargs)


def _json(response):
    try:
        return response.json()
    except Exception:
        return None


def _row_message(row: dict) -> InboundMessage:
    return InboundMessage(
        provider_id=str(row.get("provider_id") or ""),
        from_email=str(row.get("from_email") or ""),
        subject=str(row.get("subject") or ""),
        body=str(row.get("body") or ""),
        message_id=str(row.get("message_id") or ""),
        in_reply_to=str(row.get("in_reply_to") or ""),
        is_bounce=bool(row.get("is_bounce")),
    )
