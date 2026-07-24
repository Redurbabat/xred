"""Tests für das Einlesen von Gefolgt-/Follower-Listen aus Dateien."""

import json

import pytest

from app.import_lists import ImportError_, parse_account_file


def _names(accounts):
    return [a["username"] for a in accounts]


def test_instagram_following_json():
    # Struktur wie in Instagrams offiziellem Export "following.json".
    data = {
        "relationships_following": [
            {"string_list_data": [{"value": "Alice", "href": "https://instagram.com/Alice"}]},
            {"string_list_data": [{"value": "bob"}]},
        ]
    }
    accounts = parse_account_file(json.dumps(data).encode(), "following.json")
    assert _names(accounts) == ["alice", "bob"]     # kleingeschrieben
    assert all(a["full_name"] == "" and a["pic"] == "" for a in accounts)


def test_instagram_following_json_title_and_u_href():
    # Echtes "following (1).json"-Format: Name steht im Feld "title", und die
    # href-URL nutzt den "/_u/"-Präfix – KEIN "value"-Feld vorhanden.
    data = {
        "relationships_following": [
            {"title": "biharkara25",
             "string_list_data": [{"href": "https://www.instagram.com/_u/biharkara25",
                                   "timestamp": 1778492018}]},
            {"title": "zennubye",
             "string_list_data": [{"href": "https://www.instagram.com/_u/zennubye",
                                   "timestamp": 1778491388}]},
        ]
    }
    accounts = parse_account_file(json.dumps(data).encode(), "following (1).json")
    assert _names(accounts) == ["biharkara25", "zennubye"]


def test_instagram_followers_json_toplevel_list():
    # "followers_1.json" ist eine Liste auf oberster Ebene.
    data = [
        {"string_list_data": [{"value": "carol"}]},
        {"string_list_data": [{"value": "dave"}]},
    ]
    accounts = parse_account_file(json.dumps(data).encode(), "followers_1.json")
    assert _names(accounts) == ["carol", "dave"]


def test_html_export_uses_profile_links():
    html = b"""
    <html><body>
      <a href="https://www.instagram.com/nasa/">NASA</a>
      <a href="https://www.instagram.com/p/ABC123/">ein Beitrag</a>
      <a href="https://www.instagram.com/spacex">SpaceX</a>
    </body></html>
    """
    accounts = parse_account_file(html, "connections.html")
    # Der Beitrags-Link (/p/…) wird korrekt ignoriert.
    assert _names(accounts) == ["nasa", "spacex"]


def test_plain_text_one_per_line():
    txt = "alice\n@bob\n  carol  \n\n".encode()
    accounts = parse_account_file(txt, "liste.txt")
    assert _names(accounts) == ["alice", "bob", "carol"]


def test_csv_separated():
    accounts = parse_account_file(b"alice, bob; carol", "liste.csv")
    assert _names(accounts) == ["alice", "bob", "carol"]


def test_duplicates_removed_keeping_order():
    accounts = parse_account_file(b"alice\nbob\nalice\nBOB", "liste.txt")
    assert _names(accounts) == ["alice", "bob"]


def test_empty_file_raises():
    with pytest.raises(ImportError_):
        parse_account_file(b"   \n  \n", "leer.txt")


def test_invalid_names_are_dropped():
    # Zu lang / unerlaubte Zeichen werden verworfen.
    txt = ("gueltig_name\n" + "x" * 40 + "\nhat leer zeichen drin").encode()
    accounts = parse_account_file(txt, "liste.txt")
    assert "gueltig_name" in _names(accounts)
    assert "x" * 40 not in _names(accounts)
