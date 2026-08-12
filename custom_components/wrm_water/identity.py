"""Pure helper for config-entry identity (no Home Assistant deps).

Kept HA-free so the unique-id logic is unit-testable without the HA test
harness, like the api/bucketing modules.
"""

from __future__ import annotations


def normalize_base(base_url: str) -> str:
    """Canonical form of a portal base URL for comparison."""
    return base_url.rstrip("/").lower()


def entry_unique_id(base_url: str, email: str) -> str:
    """Stable, unique id for a config entry: portal URL + account email.

    The utility/tenant is the path segment of the base URL (the shared host
    wmd.wrm-systems.fi serves many utilities, each under its own path segment),
    so the full normalized base URL plus the account email uniquely identifies a
    login. This lets the same email be configured against two different
    utilities while treating the same account on the same portal as a single
    entry.
    """
    return f"{normalize_base(base_url)}::{email.lower()}"
