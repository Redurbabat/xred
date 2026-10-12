"""Tests für den globalen Herkunftsschutz (CSRF + DNS-Rebinding).

Bisher war nur der X-Teil geschützt. Die Instagram-Endpunkte nahmen jede
Anfrage an: Eine beliebige im Browser geöffnete Seite konnte im Hintergrund
den Download-Ordner umstellen, einen Komplett-Download aller Abos auslösen
oder abmelden. Die Antwort blieb für sie unlesbar, die Wirkung trat ein.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.database import Database
from app.settings import Settings
from app.web import create_app


class _Downloader:
    is_logged_in = False
    logged_in_user = None

    def backoff_status(self):
        return {"active": False, "remaining_seconds": 0}


@pytest.fixture()
def client(tmp_path: Path):
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "dl"))
    yield create_app(db, settings, _Downloader(), access_key=None).test_client()
    db.close()


def test_a_foreign_page_cannot_change_the_download_folder(client, tmp_path: Path):
    ziel = tmp_path / "vom-angreifer"
    antwort = client.post(
        "/api/settings",
        json={"download_dir": str(ziel)},
        headers={"Origin": "https://boese.example"},
    )
    assert antwort.status_code == 403
    assert not ziel.exists()  # der Ordner wurde nicht angelegt


def test_a_foreign_page_cannot_start_a_complete_download(client):
    antwort = client.post(
        "/api/download-all-complete",
        json={"count": 50},
        headers={"Origin": "https://boese.example"},
    )
    assert antwort.status_code == 403


def test_a_foreign_page_cannot_log_us_out(client):
    antwort = client.post("/api/logout", headers={"Origin": "https://boese.example"})
    assert antwort.status_code == 403


def test_a_browser_that_marks_the_request_as_cross_site_is_refused(client):
    """Moderne Browser senden Sec-Fetch-Site auch ohne Origin."""
    antwort = client.post(
        "/api/backup", headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert antwort.status_code == 403


def test_an_html_form_post_is_not_read_as_json(client):
    """Ein fremdes Formular kommt ohne JSON-Kopfzeile – und wird ignoriert.

    Zweite Verteidigungslinie: Selbst wenn die Herkunftsprüfung einmal
    danebengreift, nimmt kein Endpunkt mehr Formulardaten als JSON an.
    """
    antwort = client.post(
        "/api/settings",
        data='{"download_dir": "/tmp/boese"}',
        content_type="text/plain",
    )
    # Angenommen, aber ohne verwertbare Daten – der Ordner bleibt unberührt.
    assert antwort.status_code == 200
    assert antwort.get_json()["download_dir"] != "/tmp/boese"


def test_the_own_interface_still_works(client):
    """Same-Origin-Anfragen der eigenen Oberfläche bleiben erlaubt."""
    antwort = client.post(
        "/api/favorite",
        json={"username": "jemand", "on": True},
        headers={"Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"},
    )
    assert antwort.status_code == 200


def test_tools_without_an_origin_still_work(client):
    """curl & Co. senden keinen Origin – lokal bleibt das erlaubt."""
    assert client.post("/api/backup").status_code in (200, 500)


def test_a_foreign_name_pointing_at_localhost_is_refused(client):
    """DNS-Rebinding: fremder Name, der auf 127.0.0.1 zeigt."""
    antwort = client.get("/api/stats", headers={"Host": "angriff.example.com"})
    assert antwort.status_code == 403


def test_plain_addresses_keep_working(client):
    """Handy-Modus: Zugriff über die WLAN-IP darf nicht blockiert werden."""
    for host in ("127.0.0.1:8756", "localhost:8756", "192.168.1.50:8756",
                 "100.101.102.103", "[::1]:8756"):
        antwort = client.get("/api/stats", headers={"Host": host})
        assert antwort.status_code == 200, host


def test_an_own_name_can_be_allowed(tmp_path: Path, monkeypatch):
    """Eigene Domain/Tunnel über IGMON_ALLOWED_HOSTS freigeben."""
    monkeypatch.setenv("IGMON_ALLOWED_HOSTS", "meine-app.example, zweite.example")
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "dl"))
    client = create_app(db, settings, _Downloader(), access_key=None).test_client()
    assert client.get("/api/stats", headers={"Host": "meine-app.example"}).status_code == 200
    assert client.get("/api/stats", headers={"Host": "fremd.example"}).status_code == 403
    db.close()


def test_https_behind_a_tunnel_is_not_mistaken_for_a_foreign_origin(client):
    """Hinter Cloudflare/Reverse-Proxy nennt der Host-Header keinen Port.

    Der Origin-Kopf meint dann 443, der Host-Header scheinbar 80 – ohne
    Rücksicht auf X-Forwarded-Proto hätte der Schutz die eigene Oberfläche
    ausgesperrt.
    """
    antwort = client.post(
        "/api/favorite",
        json={"username": "jemand", "on": True},
        headers={
            "Host": "meine-app.example",
            "Origin": "https://meine-app.example",
            "X-Forwarded-Proto": "https",
            "Sec-Fetch-Site": "same-origin",
        },
    )
    # 403 käme hier NUR vom Hostnamen (nicht freigegeben), nicht vom Vergleich.
    assert antwort.status_code == 403
    assert "Adresse" in antwort.get_json()["error"]


def test_https_tunnel_works_once_the_name_is_allowed(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("IGMON_ALLOWED_HOSTS", "meine-app.example")
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "dl"))
    client = create_app(db, settings, _Downloader(), access_key=None).test_client()
    antwort = client.post(
        "/api/favorite",
        json={"username": "jemand", "on": True},
        headers={
            "Host": "meine-app.example",
            "Origin": "https://meine-app.example",
            "X-Forwarded-Proto": "https",
            "Sec-Fetch-Site": "same-origin",
        },
    )
    assert antwort.status_code == 200
    db.close()
