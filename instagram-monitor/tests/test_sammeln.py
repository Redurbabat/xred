"""Tests für das Doppelklick-Programm „sammeln" (Sammelordner aufbauen)."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from app.database import Database
from app.settings import Settings


@pytest.fixture()
def sammeln(tmp_path: Path, monkeypatch):
    """Lädt das Programm mit Daten- und Download-Ordner im Testbereich."""
    modul = importlib.import_module("sammeln")
    monkeypatch.setattr(modul, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(modul, "DOWNLOAD_DIR", tmp_path / "downloads")
    return modul


def _datenbank_anlegen(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    db = Database(tmp_path / "data" / "monitor.db")
    Settings(db, default_download_dir=str(tmp_path / "downloads"))
    db.close()


def _video(tmp_path: Path, *teile: str) -> Path:
    ziel = tmp_path / "downloads" / "X" / Path(*teile)
    ziel.parent.mkdir(parents=True, exist_ok=True)
    ziel.write_bytes(b"video")
    return ziel


def test_without_a_database_it_explains_itself(sammeln, tmp_path: Path, capsys):
    assert sammeln.main() == 1
    ausgabe = capsys.readouterr().out
    assert "keine Datenbank" in ausgabe
    assert "monitor.db" in ausgabe


def test_videos_end_up_in_the_collection_folder(sammeln, tmp_path: Path, capsys):
    _datenbank_anlegen(tmp_path)
    erstes = _video(tmp_path, "suche", "alice", "Videos", "111_alice_1.mp4")
    _video(tmp_path, "Likes", "bob", "Videos", "222_bob_1.mp4")

    assert sammeln.main() == 0
    sammel = tmp_path / "downloads" / "X" / "Alle"
    assert {p.name for p in sammel.glob("*.mp4")} == {"111_alice_1.mp4", "222_bob_1.mp4"}
    # Harte Verknüpfung: dieselbe Datei, kein zusätzlicher Platzbedarf.
    assert (sammel / "111_alice_1.mp4").stat().st_ino == erstes.stat().st_ino
    assert erstes.exists()                      # nichts verschoben
    assert "Neu im Sammelordner:    2" in capsys.readouterr().out


def test_running_twice_changes_nothing(sammeln, tmp_path: Path, capsys):
    _datenbank_anlegen(tmp_path)
    _video(tmp_path, "suche", "alice", "Videos", "111_alice_1.mp4")
    assert sammeln.main() == 0
    capsys.readouterr()

    assert sammeln.main() == 0
    ausgabe = capsys.readouterr().out
    assert "Neu im Sammelordner:    0" in ausgabe
    assert "Waren schon drin:       1" in ausgabe


def test_an_empty_download_folder_gives_a_hint(sammeln, tmp_path: Path, capsys):
    _datenbank_anlegen(tmp_path)
    assert sammeln.main() == 0
    assert "keine X-Videos gefunden" in capsys.readouterr().out
