"""Tests für die Benutzernamen-Erkennung (app/util.py)."""

from app.util import extract_shortcode, extract_username


def test_plain_name():
    assert extract_username("nasa") == "nasa"


def test_at_prefix_and_case():
    assert extract_username("@NASA") == "nasa"


def test_full_profile_url():
    assert extract_username("https://www.instagram.com/nasa/") == "nasa"


def test_url_with_query():
    assert extract_username("instagram.com/nasa/?hl=de") == "nasa"


def test_post_link_is_not_a_profile():
    # Beitrags-/Reel-Links liefern KEINEN Profilnamen.
    assert extract_username("https://www.instagram.com/p/ABC123/") == ""
    assert extract_username("instagram.com/reel/XYZ/") == ""


def test_whitespace_trimmed():
    assert extract_username("  nasa  ") == "nasa"


def test_shortcode_from_post_and_reel():
    assert extract_shortcode("https://www.instagram.com/p/ABC123/") == "ABC123"
    assert extract_shortcode("instagram.com/reel/XY_-9/") == "XY_-9"
    assert extract_shortcode("https://www.instagram.com/tv/CoDe/") == "CoDe"


def test_shortcode_with_query_and_surrounding_text():
    shared = "Schau dir das an: https://www.instagram.com/reel/AbC123/?igsh=xyz"
    assert extract_shortcode(shared) == "AbC123"


def test_shortcode_none_for_profile_or_empty():
    assert extract_shortcode("https://www.instagram.com/nasa/") == ""
    assert extract_shortcode("kein link hier") == ""
    assert extract_shortcode("") == ""
