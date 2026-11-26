"""Network helpers."""

from __future__ import annotations

import ipaddress


def valid_ip(value: str | None) -> str | None:
    """Return `value` only if it is a real IP address.

    Test clients report a hostname ("testclient"), proxies may inject arbitrary strings, and
    `audit_logs.ip` / `sessions.ip` are Postgres `inet` columns that reject non-IP text with a
    DataError. Validating here keeps bad values out of the database instead of failing a request
    at INSERT time.
    """
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None
