"""Tests for config-entry identity helpers (pure, no Home Assistant)."""

from identity import entry_unique_id, normalize_base


def test_normalize_base_strips_trailing_slash_and_lowercases():
    assert normalize_base("https://wmd.wrm-systems.fi/Site/") == (
        "https://wmd.wrm-systems.fi/site"
    )
    assert normalize_base("https://WMD.WRM-systems.fi/site") == (
        "https://wmd.wrm-systems.fi/site"
    )


def test_unique_id_combines_portal_and_email():
    assert (
        entry_unique_id("https://wmd.wrm-systems.fi/site", "User@Example.FI")
        == "https://wmd.wrm-systems.fi/site::user@example.fi"
    )


def test_unique_id_same_account_same_portal_is_stable():
    # Trailing slash / case differences must not change identity.
    a = entry_unique_id("https://wmd.wrm-systems.fi/site/", "a@b.fi")
    b = entry_unique_id("https://wmd.wrm-systems.fi/SITE", "A@B.FI")
    assert a == b


def test_unique_id_same_email_different_utility_differs():
    # The utility is the URL path segment; same email on two utilities are
    # distinct entries.
    utility_a = entry_unique_id("https://wmd.wrm-systems.fi/utility-a", "a@b.fi")
    utility_b = entry_unique_id("https://wmd.wrm-systems.fi/utility-b", "a@b.fi")
    assert utility_a != utility_b


def test_unique_id_different_email_same_utility_differs():
    one = entry_unique_id("https://wmd.wrm-systems.fi/site", "a@b.fi")
    two = entry_unique_id("https://wmd.wrm-systems.fi/site", "c@d.fi")
    assert one != two
