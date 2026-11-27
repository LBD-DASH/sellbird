"""Rule-based reply classes. The model is only asked when these rules do not match.

Classes: interested, question, not_now, stop, unsubscribe, bounce, out_of_office.
Unknown stays unknown. Nothing here invents a class from a hunch.
"""

import re
from dataclasses import dataclass

_SPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[^a-z\s]")

_BOUNCE = (
    "delivery status notification",
    "undeliverable",
    "delivery failed",
    "address not found",
    "mail delivery failed",
    "returned mail",
)
_BOUNCE_FROM = ("mailer-daemon", "postmaster")
_OOO = (
    "out of office",
    "out-of-office",
    "automatic reply",
    "auto-reply",
    "autoreply",
    "away until",
    "on annual leave",
    "on leave until",
    "i am away",
    "i'm away",
)
_NOT_NOW = (
    "not now",
    "not right now",
    "maybe later",
    "next quarter",
    "next month",
    "circle back later",
    "try me next",
)
_INTERESTED = (
    "tell me more",
    "sounds good",
    "i am interested",
    "i'm interested",
    "sign me up",
    "send it over",
    "send me the",
    "yes please",
    "count me in",
)
_QUESTION_START = ("how ", "what ", "when ", "where ", "why ", "which ", "can you ", "do you ", "could you ")


@dataclass(frozen=True)
class ReplyClass:
    kind: str
    why: str


def _flat(text: str) -> str:
    lowered = (text or "").lower().replace("\u2019", "'")
    lowered = _PUNCT.sub(" ", lowered)
    return _SPACE.sub(" ", lowered).strip()


def classify_reply(text: str, from_email: str = "", subject: str = "") -> ReplyClass:
    """Classify one reply. ``kind`` is ``unknown`` when no rule matches."""
    sender = (from_email or "").lower()
    subject_l = (subject or "").lower()
    blob = f"{subject_l}\n{(text or '').lower()}"
    if any(bit in sender for bit in _BOUNCE_FROM) or any(bit in blob for bit in _BOUNCE):
        return ReplyClass("bounce", "The message is a bounce.")
    flat = _flat(text)
    subject_flat = _flat(subject)
    if any(bit in blob for bit in _OOO):
        return ReplyClass("out_of_office", "The message is an out of office reply.")
    if flat in {"stop", "please stop", "stop please"} or subject_flat == "stop":
        return ReplyClass("stop", "The reply is STOP.")
    if "unsubscribe" in flat or "unsubscribe" in subject_flat:
        return ReplyClass("unsubscribe", "The reply asks to unsubscribe.")
    if "opt out" in flat or flat in {"remove me", "please remove me"}:
        return ReplyClass("unsubscribe", "The reply asks to be removed.")
    if any(bit in flat for bit in _NOT_NOW):
        return ReplyClass("not_now", "The reply says not now.")
    if "?" in (text or "") or any(flat.startswith(bit) for bit in _QUESTION_START):
        return ReplyClass("question", "The reply asks a question.")
    if any(bit in flat for bit in _INTERESTED) or flat in {"yes", "interested", "yep", "sure"}:
        return ReplyClass("interested", "The reply asks to hear more.")
    return ReplyClass("unknown", "No rule matched.")
