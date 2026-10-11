"""Test für den festen Zugangscode (Handy-/Tunnel-Modus).

Der Code muss über Neustarts stabil bleiben, damit ein einmal gesetztes
Handy-Lesezeichen dauerhaft funktioniert.
"""

from pathlib import Path

import web


def test_key_is_persisted_and_reused(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("IGMON_KEY", raising=False)
    first = web._stable_access_key(tmp_path)
    assert first                                   # nicht leer
    assert (tmp_path / "access_key.txt").is_file()  # gespeichert
    # Zweiter Aufruf (= „Neustart") liefert exakt denselben Code.
    assert web._stable_access_key(tmp_path) == first


def test_env_override_wins(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("IGMON_KEY", "mein-fester-code")
    assert web._stable_access_key(tmp_path) == "mein-fester-code"
