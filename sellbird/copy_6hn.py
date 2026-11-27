"""The only offers a 6 Human Needs email may name.

Wording is limited to a free 10-minute assessment, a $29 Personal report,
a $59 Relationship report, and a $149 Personal Report plus 1 hour of
coaching. No other price belongs in a draft.
"""

import re

ALLOWED_PRICES = (29, 59, 149)
_MONEY = re.compile(r"\$\s*(\d+(?:\.\d{1,2})?)")

# Replies only. Cold email is still written per person. These sentences name
# the four offers and nothing else.
REPLY_INTERESTED = (
    "You can start with the free 10-minute assessment. "
    "The paid options are a $29 Personal report, a $59 Relationship report, "
    "and a $149 Personal Report plus 1 hour of coaching. "
    "Which one should I send?"
)
REPLY_QUESTION = (
    "There are four options: a free 10-minute assessment, a $29 Personal report, "
    "a $59 Relationship report, and a $149 Personal Report plus 1 hour of coaching. "
    "Tell me which one you want."
)


def locked_reply(config, intent: str) -> str:
    """A fixed answer when this install locked the offer list, else ""."""
    prices = list(getattr(getattr(config, "writer", None), "allowed_prices", None) or [])
    if not prices:
        return ""
    if intent == "interested":
        return REPLY_INTERESTED
    if intent == "question":
        return REPLY_QUESTION
    return ""


def unexpected_prices(text: str, allowed: list[int] | tuple[int, ...] | None) -> list[str]:
    """Dollar amounts in ``text`` that are not in ``allowed``. Empty allowed skips the check."""
    if not allowed:
        return []
    ok = {int(n) for n in allowed}
    bad: list[str] = []
    for raw in _MONEY.findall(text or ""):
        number = float(raw)
        if number == int(number) and int(number) in ok:
            continue
        bad.append(f"${raw}")
    return bad
