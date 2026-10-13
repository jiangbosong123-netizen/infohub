from __future__ import annotations

"""One rule for credentials inside URLs, wherever a URL is stored, kept as evidence or served."""

import re

# Compared after lower-casing and dropping everything but letters and digits, so X-Amz-Signature,
# refresh_token and id_token all match.
_NAMES = frozenset({
    "accesstoken", "apikey", "auth", "authorization", "clientsecret", "key", "password",
    "secret", "sign", "signature", "token",
})
_SUFFIXES = (
    "authorization", "cookie", "password", "secret", "token", "apikey", "signature",
    "privatekey", "clientsecret", "accesstoken", "refreshtoken",
)


def _normalized(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.casefold())


def is_secret_key(name: str) -> bool:
    """A field of a structured record that holds a credential (``refresh_token``, ``X-Api-Key``).

    Suffixes only: plain field names like ``key`` or ``sign`` are ordinary data in source records,
    and stored evidence must keep hashing the same way.
    """
    return _normalized(name).endswith(_SUFFIXES)


def is_secret_param(name: str) -> bool:
    """A URL query parameter that carries a credential: the suffixes plus bare names (``key=``)."""
    return _normalized(name) in _NAMES or is_secret_key(name)


def without_userinfo(netloc: str) -> str:
    """``user:password@host`` -> ``host``."""
    return netloc.rsplit("@", 1)[-1]
