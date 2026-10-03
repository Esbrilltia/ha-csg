"""Non-sensitive labels for runtime diagnostics."""

import hashlib


def account_log_id(account_number: str) -> str:
    """Keep utility account numbers out of integration log messages."""
    return hashlib.sha256(account_number.encode("utf-8")).hexdigest()[:8]
