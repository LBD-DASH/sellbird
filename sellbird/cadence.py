"""Who gets a follow-up, and the hard ceiling on sends in a day.

A .za address or a South African location gets one email only. Everyone
else gets the first email, then one on day 3 and one on day 7, then nothing
further. The do-not-contact table is a separate check and always wins.
"""

import re

CADENCE_SA = "sa_one_and_day_3_7"
# Days after the first email. Step 1 is day 0.
FOLLOW_UP_DAYS = {2: 3, 3: 7}
MAX_STEPS = 3

_SA_MARKERS = (
    "south africa",
    "johannesburg",
    "cape town",
    "durban",
    "pretoria",
    "gauteng",
    "kwazulu",
    "stellenbosch",
    "bloemfontein",
    "sandton",
    "soweto",
    "polokwane",
    "pietermaritzburg",
    "gqeberha",
    "port elizabeth",
    "nelspruit",
    "mbombela",
)
_ZA_HOST = re.compile(r"(^|\.)za$", re.I)


def send_ceiling(email_config) -> int:
    """The most emails that may leave today: the lower of the two caps.

    ``max_daily_sends`` is the operator's number. ``hard_daily_cap`` is the
    ceiling that number cannot rise above. The default ceiling is 75.
    """
    hard = int(getattr(email_config, "hard_daily_cap", 75) or 75)
    daily = int(getattr(email_config, "max_daily_sends", hard) or 0)
    return max(0, min(daily, hard))


def cadence_name(email_config) -> str:
    return str(getattr(email_config, "cadence", "") or "").strip()


def is_south_africa(*parts: str) -> bool:
    """True for a .za host or a place name that is in South Africa."""
    for part in parts:
        text = (part or "").strip().lower()
        if not text:
            continue
        if "@" in text:
            host = text.rsplit("@", 1)[-1].strip().strip(".")
            if _ZA_HOST.search(host):
                return True
        host = text.split("/")[0].split()[0].strip(".")
        if _ZA_HOST.search(host):
            return True
        if any(marker in text for marker in _SA_MARKERS):
            return True
    return False


def step_allowed(step: int, *place_parts: str, cadence: str = "") -> tuple[bool, str]:
    """Whether sequence step ``step`` may be sent under ``cadence``.

    Returns (allowed, reason). An empty cadence keeps the old schedule.
    """
    if cadence != CADENCE_SA:
        return True, ""
    try:
        number = int(step)
    except (TypeError, ValueError):
        return False, "follow-up step is not a number"
    if number > MAX_STEPS:
        return False, "follow-ups stop after day 7"
    if number > 1 and is_south_africa(*place_parts):
        return False, "south africa: one email only"
    return True, ""


def send_day(step: int, cadence: str = "") -> int | None:
    """Days after the first email, or None when this cadence does not set it."""
    if cadence != CADENCE_SA:
        return None
    number = int(step)
    if number <= 1:
        return 0
    return FOLLOW_UP_DAYS.get(number)
