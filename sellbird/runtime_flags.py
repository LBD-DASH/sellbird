"""Process flags that decide whether mail leaves the machine.

SENDING_ENABLED and AUTO_REPLY_ENABLED default to off. An empty value, or
anything that is not a clear yes, stays off. There is no paid or silent
fallback: off means drafts stay queued.
"""

import logging
import os

logger = logging.getLogger("sellbird.flags")

_YES = {"1", "true", "yes", "on"}
_NO = {"0", "false", "no", "off"}


def env_flag(name: str, default: bool = False) -> bool:
    """Read a yes/no environment variable. Unset or blank uses ``default``."""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    value = str(raw).strip().lower()
    if value in _YES:
        return True
    if value in _NO:
        return False
    logger.error("%s=%r is not a yes or no. Treating it as off.", name, raw)
    return False


def sending_enabled() -> bool:
    """When false, approved drafts stay in the queue and nothing is sent."""
    return env_flag("SENDING_ENABLED", default=False)


def auto_reply_enabled() -> bool:
    """When false, drafted answers wait for a person. They are not sent."""
    return env_flag("AUTO_REPLY_ENABLED", default=False)
