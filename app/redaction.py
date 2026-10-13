from __future__ import annotations

"""Strip credentials from text that is about to be logged or stored."""

import re

from . import config


def redact(text: str) -> str:
    for secret in (config.LLM_API_KEY, config.EXTERNAL_HEARTBEAT_URL):
        if secret:
            text = text.replace(secret, "[redacted]")
    text = re.sub(r"(?i)\bauthorization\s*[:=]\s*(?:bearer\s+)?[^\s,;]+", "authorization=[redacted]", text)
    return re.sub(r"(?i)\b(token|password|api[_-]?key)\s*[:=]\s*[^\s,;]+", r"\1=[redacted]", text)
