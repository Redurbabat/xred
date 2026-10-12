"""
Lokale Web-Oberfläche (Flask).
==============================

Stellt eine Browser-Oberfläche bereit, die sich wie ein Instagram-Feed
anfühlt: Konto eingeben → Beiträge als Bilder-Raster ansehen → einzelne
Fotos/Videos per Klick herunterladen.

WICHTIG: Das ist bewusst KEINE öffentliche Webseite. Der Server bindet
sich nur an 127.0.0.1 (localhost) und ist damit ausschließlich vom
eigenen Rechner erreichbar. Unter der Haube arbeitet derselbe
Instaloader-Unterbau wie in der Desktop-App – mit denselben Grundsätzen
(öffentliche Profile bzw. eigener Login, keine Umgehung von
Zugriffsbeschränkungen, Rate-Limits werden respektiert).

Endpunkte:
  GET  /                    – die Oberfläche (index.html)
  GET  /api/status          – Anmeldestatus + Download-Ordner
  POST /api/login           – Anmeldung (JSON: username, password, code?)
  POST /api/logout          – Abmeldung
  GET  /api/posts           – neueste Beiträge eines Kontos (?username=&count=)
  GET  /thumb/<shortcode>   – Vorschaubild-Proxy (umgeht Hotlink-Probleme)
  POST /api/download        – einen Beitrag herunterladen (JSON: shortcode)
"""

from __future__ import annotations

import datetime
import hashlib
import ipaddress
import json
import logging
import os
import random
import re
import secrets
import shutil
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Optional
from urllib.parse import quote, urlsplit

import requests as _requests
from flask import (
    Flask,
    Response,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
)
from markupsafe import escape as _escape

from .downloader import (
    DownloaderError,
    LoginError,
    PostInfo,
    ProfileIsPrivate,
    ProfileNotFound,
    TemporaryError,
    TwoFactorRequired,
)
from .import_lists import ImportError_, parse_account_file, parse_export_zip
from .social_cache import SocialCache
from .util import extract_shortcode, extract_username

if TYPE_CHECKING:
    from .database import Database
    from .downloader import InstagramDownloader
    from .settings import Settings
    from .x_service import XService

logger = logging.getLogger(__name__)

# Wie viele zuletzt angezeigte Beiträge im Speicher gehalten werden
# (für Vorschaubild-Proxy und Download ohne erneute Netzanfrage).
_CACHE_SIZE = 500

# Obergrenze für die Anzahl abrufbarer Beiträge pro Anfrage.
_MAX_COUNT = 100
# „Komplett"-Modus: praktisch unbegrenzt viele Beiträge je Konto.
_COMPLETE_COUNT = 10_000

# Pause zwischen zwei Konten beim Massen-Download (Sekunden) – rücksichtsvoll
# gegenüber den Instagram-Servern.
_BULK_DELAY = 4.0

# Pause zwischen zwei Profilen beim Feed-Aufbau (Sekunden). Kürzer als beim
# Download, damit der Feed zügig erscheint, aber immer noch rücksichtsvoll.
_FEED_DELAY = 1.5

# Namen, unter denen der eigene PC sich selbst erreicht. Nur diese dürfen den
# X-Passwort-Endpunkt ansprechen – jeder andere Host-Header wäre entweder ein
# Gerät im WLAN oder ein DNS-Rebinding-Versuch von einer fremden Seite aus.
_LOCAL_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def _header_hostname(value: str) -> str:
    """Extrahiert den reinen Hostnamen aus „host:port“ bzw. „[::1]:port“."""
    host = (value or "").strip().rstrip(".")
    if not host:
        return ""
    if host.startswith("["):
        end = host.find("]")
        return host[1:end].lower() if end > 0 else ""
    return host.split(":", 1)[0].lower()


def _is_local_host_header(value: str) -> bool:
    """Schutz vor DNS-Rebinding: nur localhost-Namen sind zulässig."""
    return _header_hostname(value) in _LOCAL_HOSTNAMES


def _is_local_origin(value: str) -> bool:
    """Prüft Origin/Referer einer Anfrage (CSRF-Schutz)."""
    if not value or value == "null":
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    return (parsed.hostname or "").lower() in _LOCAL_HOSTNAMES


def _authority(value: str, *, is_url: bool = False, default_port: int = 80) -> str:
    """Normalisiert „host:port“ zum Vergleich von Origin und Host.

    Wird für den Herkunftsvergleich gebraucht: Verglichen wird NICHT gegen
    localhost, sondern gegen den Host der Anfrage selbst. Auf dem PC ist das
    127.0.0.1, im Handy-Modus die WLAN-Adresse, über Tailscale die
    100.x-Adresse – die Prüfung stimmt damit in allen Betriebsarten.

    ``default_port`` gilt für einen Host-Header ohne Portangabe. Hinter
    einem HTTPS-Tunnel (Cloudflare, Reverse Proxy) steht dort nur der Name,
    während der Origin-Kopf „https://name“ = Port 443 meint. Ohne diesen
    Parameter verglichen sich 443 und 80 – und jede schreibende Anfrage
    wäre mit 403 abgewiesen worden.
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    if is_url:
        try:
            parsed = urlsplit(raw)
        except ValueError:
            return ""
        if parsed.scheme not in ("http", "https"):
            return ""
        host = (parsed.hostname or "").lower().rstrip(".")
        if not host:
            return ""
        default = 443 if parsed.scheme == "https" else 80
        try:
            port = parsed.port or default
        except ValueError:
            return ""
        return f"{host}:{port}"
    host = _header_hostname(raw)
    if not host:
        return ""
    tail = raw.rsplit("]", 1)[-1] if raw.startswith("[") else raw
    port_part = tail.split(":", 1)[1].strip() if ":" in tail else ""
    if port_part:
        if not port_part.isdigit():
            return ""
        return f"{host}:{int(port_part)}"
    return f"{host}:{default_port}"


def _forwarded_scheme(headers, fallback: str) -> str:
    """http/https der ursprünglichen Anfrage – auch hinter einem Proxy."""
    raw = (headers.get("X-Forwarded-Proto", "") or "").split(",")[0].strip().lower()
    return raw if raw in ("http", "https") else fallback


class _FeedJob:
    """Baut fortlaufend den Abo-Feed auf (Beiträge mehrerer Profile)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.thread: Optional[threading.Thread] = None
        self.cancel = False
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.running = False
            self.cancel = False
            self.total = 0          # Anzahl Profile
            self.done = 0           # fertige Profile
            self.current = ""       # gerade geladenes Profil
            self.items: list[dict] = []   # gesammelte Beiträge (JSON-fertig)
            self.finished = False
            self.error = ""

    def add_items(self, new_items: list[dict]) -> None:
        with self._lock:
            self.items.extend(new_items)

    def snapshot(self, include_items: bool = True) -> dict:
        with self._lock:
            data = {
                "running": self.running,
                "total": self.total,
                "done": self.done,
                "current": self.current,
                "finished": self.finished,
                "error": self.error,
                "count": len(self.items),
            }
            if include_items:
                # Neueste zuerst – über alle Profile hinweg.
                data["items"] = sorted(
                    self.items, key=lambda x: x.get("posted_at", ""), reverse=True
                )
            return data

    def update(self, **kwargs) -> None:
        with self._lock:
            for key, value in kwargs.items():
                setattr(self, key, value)


class _BulkJob:
    """Fortschritts-Zustand des Massen-Downloads (thread-sicher)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.thread: Optional[threading.Thread] = None
        self.cancel = False
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._reset_locked()

    def _reset_locked(self) -> None:
        self.running = False
        self.cancel = False
        self.total = 0          # Anzahl Konten
        self.done = 0           # fertige Konten
        self.current = ""       # gerade bearbeitetes Konto
        self.downloaded = 0     # gespeicherte Beiträge gesamt
        self.finished = False
        self.error = ""
        # Warteschlange je Konto, damit die Oberfläche zeigen kann, WAS
        # gerade läuft: [{"name", "state": "pending|active|done", "n"}].
        self.items = []

    def try_start(self, total: int = 0, current: str = "") -> bool:
        """Startet den Job ATOMAR: prüft „läuft schon?" und setzt „läuft"
        unter EINEM gehaltenen Lock. Verhindert, dass zwei Auslöser
        (z. B. Zeitplan um 12:00 UND ein Klick) gleichzeitig zwei Worker
        auf demselben Zustand starten. Rückgabe: False, wenn schon läuft."""
        with self._lock:
            if self.running:
                return False
            self._reset_locked()
            self.running = True
            self.total = total
            self.current = current
            return True

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "running": self.running,
                "total": self.total,
                "done": self.done,
                "current": self.current,
                "downloaded": self.downloaded,
                "finished": self.finished,
                "error": self.error,
                "items": [dict(it) for it in self.items],
            }

    def update(self, **kwargs) -> None:
        with self._lock:
            for key, value in kwargs.items():
                setattr(self, key, value)

    def set_items(self, names: list[str]) -> None:
        """Legt die Warteschlange an (ein Eintrag je Konto)."""
        with self._lock:
            self.items = [{"name": n, "state": "pending", "n": 0} for n in names]

    def mark_item(self, index: int, state: Optional[str] = None,
                  n: Optional[int] = None) -> None:
        """Setzt Zustand/Zähler eines Eintrags (thread-sicher)."""
        with self._lock:
            if 0 <= index < len(self.items):
                if state is not None:
                    self.items[index]["state"] = state
                if n is not None:
                    self.items[index]["n"] = n


def create_app(
    db: "Database",
    settings: "Settings",
    downloader: "InstagramDownloader",
    access_key: Optional[str] = None,
    x_service: Optional["XService"] = None,
) -> Flask:
    """Erzeugt die Flask-Anwendung mit allen Endpunkten.

    ``access_key`` schützt den Zugriff, wenn die Seite über das Netzwerk
    (Handy im WLAN) erreichbar ist: Ohne gültigen Code gibt es nur eine
    Hinweisseite. Zugriffe von localhost (dem PC selbst) sind immer
    erlaubt. Ist ``access_key`` None, gibt es keinen Schutz (reiner
    localhost-Betrieb).
    """
    app = Flask(__name__)
    # CSS/JS liegen jetzt unter app/static/ (aus index.html ausgelagert).
    # Kein Langzeit-Cache: Nach einem Update reicht ein normales Neuladen
    # (F5) – die Oberfläche zeigt sofort die neue Version, kein Strg+F5 nötig.
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

    # Zuletzt geholte Beiträge: shortcode → PostInfo (LRU-artig begrenzt).
    # Der Server läuft mit threaded=True; auf den Cache greifen sowohl
    # Request-Threads als auch der Feed-Worker-Thread zu → mit Lock schützen,
    # sonst drohen KeyError/verlorene Einträge bei gleichzeitigem Zugriff.
    cache: "OrderedDict[str, PostInfo]" = OrderedDict()
    cache_lock = threading.Lock()

    # Gefolgt-/Follower-Zwischenspeicher: hält Listen und Profilbilder
    # lokal aktuell, damit die Oberfläche beim Öffnen sofort alles hat.
    # ------------------------------------------------------------------
    # Mitgelieferte Start-Listen: Beim allerersten Start (leere Datenbank,
    # z. B. frisch installierte Android-App) werden Gefolgt/Follower aus
    # app/seed_lists.json vorbefüllt – so ist sofort alles da, ganz ohne
    # Import und ohne Instagram-Abruf. (Bewusst mit eingecheckt; die Datei
    # enthält nur Benutzernamen.)
    # ------------------------------------------------------------------
    try:
        _seed_path = Path(__file__).resolve().parent / "seed_lists.json"
        if _seed_path.is_file():
            _seed = json.loads(_seed_path.read_text(encoding="utf-8"))
            for _kind in ("following", "followers"):
                _names = _seed.get(_kind) or []
                if _names and not db.get_social_list(_kind):
                    db.replace_social_list(_kind, [
                        {"username": str(n), "full_name": "", "pic": ""}
                        for n in _names if str(n).strip()
                    ])
                    logger.info("Startliste „%s“ vorbefüllt: %d Konten.",
                                _kind, len(_names))
    except Exception:
        logger.debug("Startlisten konnten nicht geladen werden.", exc_info=True)

    social = SocialCache(
        db, settings, downloader,
        avatar_dir=db.path.parent / "cache" / "avatars",
    )
    social.start()

    # ------------------------------------------------------------------
    # Sofort-Feed: Die zuletzt geladenen Feed-Beiträge werden lokal
    # gespeichert (JSON) und beim Öffnen SOFORT angezeigt – wie bei
    # Instagram. Frisch geladen wird erst danach (auf ⟳ bzw. beim
    # nächsten Feed-Lauf), die Datei aktualisiert sich dabei mit.
    # ------------------------------------------------------------------
    _feed_store = db.path.parent / "cache" / "feed.json"
    _feed_store_lock = threading.Lock()

    def _load_cached_feed() -> dict:
        try:
            data = json.loads(_feed_store.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                return data
        except Exception:
            pass
        return {"saved_at": "", "items": []}

    def _save_feed_items(new_items: list[dict]) -> None:
        """Mischt frische Beiträge in den Sofort-Feed (neueste 300 bleiben)."""
        if not new_items:
            return
        try:
            with _feed_store_lock:
                data = _load_cached_feed()
                by_code = {i.get("shortcode"): i for i in data["items"]
                           if i.get("shortcode")}
                for item in new_items:
                    code = item.get("shortcode")
                    if code:
                        by_code[code] = item
                items = sorted(by_code.values(),
                               key=lambda x: x.get("posted_at", ""),
                               reverse=True)[:300]
                payload = {
                    # UTC mit Offset: Die Oberfläche zeigt diesen Wert über
                    # timeAgo an und liest zeitzonenlose Werte als UTC.
                    "saved_at": datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(timespec="seconds"),
                    "items": items,
                }
                _feed_store.parent.mkdir(parents=True, exist_ok=True)
                tmp = _feed_store.with_suffix(".tmp")
                tmp.write_text(json.dumps(payload, ensure_ascii=False),
                               encoding="utf-8")
                tmp.replace(_feed_store)
        except OSError:
            logger.debug("Sofort-Feed konnte nicht gespeichert werden.",
                         exc_info=True)

    # ------------------------------------------------------------------
    # Zugangsschutz (nur aktiv, wenn ein access_key gesetzt ist)
    # ------------------------------------------------------------------
    _COOKIE = "igmon_key"

    def _is_local(addr: str | None) -> bool:
        return addr in ("127.0.0.1", "::1", None)

    @app.before_request
    def _guard():
        if not access_key:
            return None  # kein Schutz im reinen localhost-Betrieb
        if _is_local(request.remote_addr):
            return None  # der PC selbst darf immer
        # Code per URL (?key=…) übergeben → als Cookie merken und sauber
        # weiterleiten (damit der Code nicht in jeder URL steht).
        provided = request.args.get("key")
        if provided and provided == access_key:
            resp = redirect(request.path or "/")
            resp.set_cookie(_COOKIE, access_key, max_age=30 * 24 * 3600,
                            samesite="Lax")
            return resp
        if request.cookies.get(_COOKIE) == access_key:
            return None
        # Kein gültiger Code → freundliche Hinweisseite (403).
        return Response(
            "<h2>Zugangscode nötig</h2><p>Diese Seite ist geschützt. Bitte "
            "die vollständige Adresse inklusive <code>?key=…</code> "
            "verwenden, die im PC-Fenster angezeigt wird.</p>",
            status=403,
            mimetype="text/html; charset=utf-8",
        )

    # ------------------------------------------------------------------
    # Herkunftsschutz für ALLE Endpunkte (CSRF + DNS-Rebinding)
    # ------------------------------------------------------------------
    # Bisher hatten nur die X-Endpunkte diesen Schutz. Die Instagram-Seite
    # nahm jede Anfrage an: Jede Webseite, die im Browser offen ist, während
    # die App läuft, konnte per Hintergrund-Formular den Download-Ordner
    # umstellen, einen Komplett-Download aller Abos auslösen (= Instagram-
    # Drosselung) oder abmelden. Die Antwort bleibt für die fremde Seite
    # unlesbar – die Nebenwirkung trat trotzdem ein.
    #
    # Zusätzliche Hostnamen (eigene Domain, Tunnel, Tailscale-Name) lassen
    # sich über die Umgebungsvariable IGMON_ALLOWED_HOSTS freigeben,
    # mehrere durch Komma getrennt.
    _allowed_hosts = frozenset(
        part.strip().lower()
        for part in os.environ.get("IGMON_ALLOWED_HOSTS", "").split(",")
        if part.strip()
    )

    def _host_is_trusted(value: str) -> bool:
        """Nur localhost, reine IP-Adressen und freigegebene Namen.

        Gegen DNS-Rebinding: Eine fremde Seite kann ihren eigenen Namen auf
        127.0.0.1 zeigen lassen und wäre dann „same-origin“ – sie dürfte die
        Antworten also auch LESEN. Ein Name, der nicht freigegeben ist,
        fliegt deshalb hier raus; IP-Adressen sind davon nicht betroffen,
        weil sie sich nicht umbiegen lassen.
        """
        host = _header_hostname(value)
        if not host:
            return False
        if host in _LOCAL_HOSTNAMES or host in _allowed_hosts:
            return True
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return False
        return True

    @app.before_request
    def _origin_guard():
        host = request.headers.get("Host", "")
        if host and not _host_is_trusted(host):
            return jsonify(
                error="Unerwartete Adresse. Bitte die IP-Adresse verwenden, die "
                      "im PC-Fenster steht. Eigene Namen (Domain/Tunnel) müssen "
                      "in IGMON_ALLOWED_HOSTS freigegeben werden.",
            ), 403
        # Lesende Anfragen ändern nichts. Wichtig auch für GET /save: Das
        # Lesezeichen und das Teilen-Ziel rufen es absichtlich von
        # instagram.com aus auf, also fremdstämmig.
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        fetch_site = request.headers.get("Sec-Fetch-Site", "")
        if fetch_site and fetch_site not in ("same-origin", "none"):
            return jsonify(error="Anfrage von einer fremden Seite abgelehnt."), 403
        source = request.headers.get("Origin", "") or request.headers.get("Referer", "")
        if not source:
            # Ein Browser kann einen fremdstämmigen POST nicht ohne Origin
            # erzeugen. Werkzeuge wie curl dürfen weiterhin lokal arbeiten.
            return None
        scheme = _forwarded_scheme(request.headers, request.scheme)
        expected = _authority(host, default_port=443 if scheme == "https" else 80)
        if not expected or _authority(source, is_url=True) != expected:
            return jsonify(error="Anfrage von einer fremden Seite abgelehnt."), 403
        return None

    def remember(posts: list[PostInfo]) -> None:
        with cache_lock:
            for post in posts:
                cache[post.shortcode] = post
                cache.move_to_end(post.shortcode)
            while len(cache) > _CACHE_SIZE:
                cache.popitem(last=False)

    def cache_get(shortcode: str) -> Optional[PostInfo]:
        """Thread-sicherer Lesezugriff auf den Beitrags-Cache."""
        with cache_lock:
            return cache.get(shortcode)

    def lookup_post(shortcode: str) -> Optional[PostInfo]:
        """Findet einen Beitrag – auch nach einem Server-Neustart.

        Reihenfolge: flüchtiger In-Memory-Cache → persistierter Sofort-Feed
        (feed.json) → Datenbank. Damit funktionieren Anzeigen, Streamen und
        Herunterladen auch dann noch, wenn der Cache nach einem Neustart leer
        ist (der Feed zeigt seine Bilder aus dem Disk-Cache weiter an). Die
        rekonstruierten Objekte haben ``raw=None``; der Downloader lädt das
        Post-Objekt dann bei Bedarf selbst per ``Post.from_shortcode`` nach.
        """
        post = cache_get(shortcode)
        if post is not None:
            return post
        # 1. Persistierter Sofort-Feed (enthält Konto, Typ, Zeitpunkt, URL).
        for item in _load_cached_feed().get("items", []):
            if item.get("shortcode") == shortcode and item.get("username"):
                return PostInfo(
                    shortcode=shortcode,
                    username=item["username"],
                    post_type=item.get("type", "") or "",
                    posted_at=item.get("posted_at", "") or "",
                    url=item.get("url")
                    or f"https://www.instagram.com/p/{shortcode}/",
                )
        # 2. Datenbank (Verlauf/registrierte Beiträge).
        row = db.find_post(shortcode)
        if row is not None and row["username"]:
            return PostInfo(
                shortcode=shortcode,
                username=row["username"],
                post_type=row["post_type"] or "",
                posted_at=row["posted_at"] or "",
                url=f"https://www.instagram.com/p/{shortcode}/",
            )
        return None

    # Auf der Platte vorhandene Beiträge erkennen (nicht nur laut Datenbank):
    # Nach einer App-Neuinstallation ist die Datenbank leer, die Dateien
    # liegen aber weiter unter Pictures/InstagramMonitor/<konto>/… – sonst
    # würden bereits geladene Beiträge fälschlich als „nicht geladen" (rot)
    # angezeigt. Der Dateiname beginnt mit dem Shortcode (z. B. „ABC_1.jpg").
    _disk_cache: dict[str, tuple[float, set[str]]] = {}
    _disk_cache_lock = threading.Lock()

    def _disk_shortcodes(username: str) -> set[str]:
        now = time.time()
        with _disk_cache_lock:
            hit = _disk_cache.get(username)
            if hit and hit[0] > now:
                return hit[1]
        found: set[str] = set()
        try:
            root = settings.download_dir / username
            if root.is_dir():
                for f in root.rglob("*"):
                    if f.is_file():
                        found.add(re.split(r"[._]", f.name, maxsplit=1)[0])
        except Exception:
            pass
        with _disk_cache_lock:
            _disk_cache[username] = (now + 30.0, found)   # 30 s zwischenspeichern
        return found

    def post_json(post: PostInfo, username: str) -> dict:
        """Serialisiert einen Beitrag für die Oberfläche (Feed & Einzelansicht)."""
        ptype = post.post_type or ""
        is_video = ("Video" in ptype) or ("Reel" in ptype)
        is_album = ptype.startswith("Album")
        downloaded = (db.is_downloaded(post.shortcode, username)
                      or post.shortcode in _disk_shortcodes(username))
        return {
            "shortcode": post.shortcode,
            "username": username,
            "type": ptype,
            "is_video": is_video,
            "is_album": is_album,
            "posted_at": post.posted_at,
            "url": post.url,
            "caption": post.caption,
            "likes": getattr(post, "likes", 0),
            "comments": getattr(post, "comments", 0),
            # Vorschaubild über unseren Proxy, nicht direkt vom CDN.
            "thumb": f"/thumb/{post.shortcode}" if post.thumbnail_url else "",
            # Direkter Geräte-Download (streamt an Handy/Browser).
            "media": f"/media/{post.shortcode}",
            "downloaded": downloaded,
        }

    # ------------------------------------------------------------------
    # Oberfläche
    # ------------------------------------------------------------------
    @app.get("/")
    def index():
        resp = make_response(render_template("index.html"))
        # Die Oberfläche selbst nie im Browser-Cache halten: Nach einem
        # Update reicht so ein normales Neuladen (F5) – kein Strg+F5 nötig.
        resp.headers["Cache-Control"] = "no-store"
        return resp

    # ------------------------------------------------------------------
    # Status & Anmeldung
    # ------------------------------------------------------------------
    @app.get("/api/status")
    def status():
        refresh = social.status()
        backoff = downloader.backoff_status()
        warning = ""
        if refresh.get("checkpoint"):
            warning = ("Instagram verlangt eine Sicherheitsbestätigung: Bitte "
                       "in deinem normalen Browser bei instagram.com anmelden, "
                       "die Sicherheitsabfrage bestätigen und hier „Aus Browser "
                       "übernehmen“ klicken.")
        elif refresh.get("session_invalid"):
            warning = ("Deine Instagram-Anmeldung ist abgelaufen – bitte neu "
                       "anmelden (z. B. „Aus Browser übernehmen“).")
        elif backoff.get("active"):
            minutes = int(backoff.get("remaining_seconds", 0) / 60) + 1
            warning = (f"Instagram drosselt gerade – Abrufe pausieren noch "
                       f"ca. {minutes} Minute(n).")
        return jsonify(
            logged_in=downloader.is_logged_in,
            user=downloader.logged_in_user or "",
            download_dir=str(settings.download_dir),
            warning=warning,
        )

    # ------------------------------------------------------------------
    # X-Videos (Desktop: sichtbare lokale Browser-Sitzung; Android: aus)
    # ------------------------------------------------------------------
    _x_android_message = (
        "X-Suche ist in der Android-App nicht verfügbar. Starte den Server "
        "auf dem PC und öffne ihn vom Handy."
    )

    def _x_unavailable():
        return jsonify(error=_x_android_message, available=False), 501

    def _x_error(exc: BaseException, status_code: int = 400):
        message = x_service.public_error(exc) if x_service is not None else _x_android_message
        return jsonify(error=message), status_code

    # ------------------------------------------------------------------
    # Schutz des X-Passwort-Endpunkts
    # ------------------------------------------------------------------
    # Das X-Passwort darf ausschließlich vom PC selbst kommen, auf dem der
    # sichtbare Browser läuft. Ein Handy/Tablet im WLAN – auch mit gültigem
    # Zugangscode – bekommt hier immer 403. Zusätzlich schützt ein Token
    # gegen CSRF: Eine fremde Webseite kann es nicht auslesen (Same-Origin-
    # Policy) und deshalb auch keine Anmeldung in deinem Namen auslösen.
    _x_csrf_token = secrets.token_urlsafe(32)

    def _same_origin_request():
        """None, wenn die Anfrage von DIESER Oberfläche kommt, sonst 403.

        Ohne diese Prüfung kann jede besuchte Webseite auf dem PC ein
        X-Browserfenster öffnen, Suchen starten oder Videos herunterladen –
        die Antwort bleibt für sie unlesbar, die Nebenwirkung tritt trotzdem
        ein. Bewusst KEINE localhost-Prüfung wie beim Passwort: Suche und
        Download sollen im Handy-Modus weiter über das WLAN funktionieren.
        """
        fetch_site = request.headers.get("Sec-Fetch-Site", "")
        if fetch_site and fetch_site not in ("same-origin", "none"):
            return jsonify(error="Anfrage von einer fremden Seite abgelehnt."), 403
        source = request.headers.get("Origin", "") or request.headers.get("Referer", "")
        if not source:
            # Ein Browser kann einen Cross-Site-POST nicht ohne Origin
            # erzeugen. Werkzeuge wie curl dürfen weiterhin lokal arbeiten.
            return None
        scheme = _forwarded_scheme(request.headers, request.scheme)
        expected = _authority(
            request.headers.get("Host", ""),
            default_port=443 if scheme == "https" else 80,
        )
        if not expected or _authority(source, is_url=True) != expected:
            return jsonify(error="Anfrage von einer fremden Seite abgelehnt."), 403
        return None

    def _json_object():
        """(Daten, None) oder (None, 400). Nie eine Liste, nie ein 503.

        ``get_json(force=True) or {}`` lieferte bei einem JSON-Array die Liste
        selbst; das folgende ``data.get(...)`` warf dann einen AttributeError,
        der als HTTP 503 mit internem Python-Text beim Nutzer landete.
        """
        try:
            data = request.get_json(silent=True)
        except Exception:
            data = None
        if data is None:
            return {}, None
        if not isinstance(data, dict):
            return None, (jsonify(error="Ungültiges Anfrageformat."), 400)
        return data, None

    def _local_only_credentials():
        """Gibt None zurück, wenn die Anfrage vom PC selbst stammt, sonst 403."""
        if not _is_local(request.remote_addr):
            return jsonify(
                error="Das X-Passwort kann nur direkt am PC eingegeben werden, nicht über "
                      "das Netzwerk (Handy/Tablet). Bitte die Oberfläche auf dem PC öffnen.",
                local_only=True,
            ), 403
        if not _is_local_host_header(request.headers.get("Host", "")):
            return jsonify(
                error="Ungültige Adresse für die X-Anmeldung. Bitte http://127.0.0.1 "
                      "verwenden.",
                local_only=True,
            ), 403
        origin = request.headers.get("Origin", "")
        referer = request.headers.get("Referer", "")
        if origin:
            if not _is_local_origin(origin):
                return jsonify(error="Anfrage von einer fremden Seite abgelehnt."), 403
        elif referer and not _is_local_origin(referer):
            return jsonify(error="Anfrage von einer fremden Seite abgelehnt."), 403
        if not request.is_json:
            return jsonify(error="Ungültiges Anfrageformat."), 403
        sent = request.headers.get("X-CSRF-Token", "")
        # isascii(): Werkzeug liest Kopfzeilen als Latin-1, ein Byte ab 0x80
        # wird also zu einem Nicht-ASCII-Zeichen. compare_digest würde damit
        # mit TypeError abbrechen (HTTP 500) – das echte Token ist reines
        # ASCII, ein solcher Wert ist deshalb schlicht falsch.
        if not sent or not sent.isascii() or not secrets.compare_digest(sent, _x_csrf_token):
            return jsonify(
                error="Sicherheitsprüfung fehlgeschlagen. Bitte die Seite neu laden (F5).",
            ), 403
        return None

    @app.get("/api/x/csrf")
    def x_csrf():
        """Liefert das Anmelde-Token – ausschließlich an den PC selbst."""
        if not _is_local(request.remote_addr) or not _is_local_host_header(
            request.headers.get("Host", "")
        ):
            return jsonify(error="Nur lokal verfügbar.", local_only=True), 403
        return jsonify(token=_x_csrf_token)

    @app.get("/api/x/status")
    def x_status():
        if x_service is None:
            idle = {
                "running": False, "finished": False, "cancelled": False,
                "total": 0, "done": 0, "found": 0, "downloaded": 0,
                "skipped": 0, "failed": 0, "current": "", "message": "",
                "error": "", "items": [],
            }
            return jsonify(
                available=False,
                browser_open=False,
                session_confirmed=False,
                local=False,
                warning=_x_android_message,
                search=idle,
                download=idle,
                recent_downloads=[],
            )
        # „local“ steuert nur die Anzeige: Auf Handy/Tablet blendet die
        # Oberfläche das Passwortfeld aus – abgelehnt wird ein Versuch
        # ohnehin serverseitig mit 403.
        return jsonify({**x_service.status(), "local": _is_local(request.remote_addr)})

    @app.post("/api/x/login/open")
    def x_login_open():
        if x_service is None:
            return _x_unavailable()
        denied = _same_origin_request()
        if denied is not None:
            return denied
        try:
            return jsonify(x_service.open_login())
        except Exception as exc:
            return _x_error(exc, 503)

    @app.post("/api/x/login")
    def x_login():
        """Direkte X-Anmeldung im sichtbaren Browser (nur vom PC selbst).

        Das Passwort wird ausschließlich an das X-Formular weitergereicht:
        Es wird nicht gespeichert, nicht protokolliert und nicht
        zurückgegeben. 2FA, CAPTCHA und Sicherheitsabfragen bleiben im
        sichtbaren Fenster.
        """
        if x_service is None:
            return _x_unavailable()
        denied = _local_only_credentials()
        if denied is not None:
            return denied
        from .x_service import XLoginError, XValidationError

        data = request.get_json(silent=True, cache=False)
        if not isinstance(data, dict):
            return jsonify(error="Ungültige Anmeldedaten."), 400
        try:
            result = x_service.login(
                data.get("identifier"),
                data.get("password"),
                data.get("username", ""),
            )
        except (XValidationError, TypeError, ValueError) as exc:
            return _x_error(exc, 400)
        except XLoginError as exc:
            return _x_error(exc, 401)
        except Exception as exc:
            return _x_error(exc, 503)
        finally:
            # Das Passwort soll nicht länger als nötig im Speicher stehen.
            data.clear()
        return jsonify(result)

    @app.post("/api/x/tokens")
    def x_login_tokens():
        """Bestehende X-Sitzung aus auth_token + ct0 übernehmen.

        Wie beim Passwort nur vom PC selbst: Die beiden Cookies sind der
        volle Zugang zum Konto. Gespeichert werden sie im Profilordner,
        zurückgegeben werden sie nie.
        """
        if x_service is None:
            return _x_unavailable()
        denied = _local_only_credentials()
        if denied is not None:
            return denied
        from .x_service import XValidationError

        data = request.get_json(silent=True, cache=False)
        if not isinstance(data, dict):
            return jsonify(error="Ungültige Eingabe."), 400
        try:
            result = x_service.login_with_tokens(data.get("auth_token"), data.get("ct0"))
        except (XValidationError, TypeError, ValueError) as exc:
            return _x_error(exc, 400)
        except Exception as exc:
            return _x_error(exc, 503)
        finally:
            data.clear()
        return jsonify(result)

    @app.post("/api/x/tokens/forget")
    def x_forget_tokens():
        if x_service is None:
            return _x_unavailable()
        denied = _local_only_credentials()
        if denied is not None:
            return denied
        return jsonify(ok=True, removed=x_service.forget_tokens())

    @app.post("/api/x/search")
    def x_search_start():
        if x_service is None:
            return _x_unavailable()
        denied = _same_origin_request()
        if denied is not None:
            return denied
        data, bad = _json_object()
        if bad is not None:
            return bad
        try:
            started = x_service.start_search(
                data.get("query"),
                data.get("limit", 10),
                data.get("sort", "latest"),
                data.get("exclude_reposts", False),
                data.get("source", "search"),
            )
        except (TypeError, ValueError) as exc:
            return _x_error(exc, 400)
        except Exception as exc:
            return _x_error(exc, 503)
        if not started:
            return jsonify(error="Es läuft bereits eine X-Suche oder ein X-Download."), 409
        return jsonify(ok=True), 202

    @app.get("/api/x/search/status")
    def x_search_status():
        if x_service is None:
            return _x_unavailable()
        return jsonify(x_service.search_job.snapshot())

    @app.post("/api/x/search/cancel")
    def x_search_cancel():
        if x_service is None:
            return _x_unavailable()
        denied = _same_origin_request()
        if denied is not None:
            return denied
        return jsonify(ok=x_service.cancel_search())

    def _start_x_download(data: dict, *, single: bool = False):
        if x_service is None:
            return _x_unavailable()
        try:
            if single:
                post_id = x_service.resolve_post_id(
                    post_id=data.get("post_id"), post_url=data.get("post_url")
                )
                started = x_service.start_download([post_id])
            else:
                post_ids = data.get("post_ids") or []
                if not isinstance(post_ids, list):
                    raise ValueError("post_ids muss eine Liste sein.")
                started = x_service.start_download(
                    post_ids, all_new=bool(data.get("all_new", False))
                )
        except (TypeError, ValueError) as exc:
            return _x_error(exc, 400)
        except Exception as exc:
            return _x_error(exc, 503)
        if not started:
            return jsonify(error="Es läuft bereits eine X-Suche oder ein X-Download."), 409
        return jsonify(ok=True), 202

    @app.post("/api/x/download")
    def x_download_one():
        denied = _same_origin_request()
        if denied is not None:
            return denied
        data, bad = _json_object()
        if bad is not None:
            return bad
        return _start_x_download(data, single=True)

    @app.post("/api/x/download-link")
    def x_download_link():
        """Beliebigen X-Beitragslink herunterladen, auch ohne Suche."""
        if x_service is None:
            return _x_unavailable()
        denied = _same_origin_request()
        if denied is not None:
            return denied
        data, bad = _json_object()
        if bad is not None:
            return bad
        try:
            post_id = x_service.remember_link(data.get("url"))
        except (TypeError, ValueError) as exc:
            return _x_error(exc, 400)
        except Exception as exc:
            return _x_error(exc, 503)
        if not x_service.start_download([post_id]):
            return jsonify(error="Es läuft bereits eine X-Suche oder ein X-Download."), 409
        return jsonify(ok=True, post_id=post_id), 202

    @app.post("/api/x/download-selected")
    def x_download_selected():
        denied = _same_origin_request()
        if denied is not None:
            return denied
        data, bad = _json_object()
        if bad is not None:
            return bad
        return _start_x_download(data)

    @app.get("/api/x/download/status")
    def x_download_status():
        if x_service is None:
            return _x_unavailable()
        return jsonify(x_service.download_job.snapshot())

    @app.post("/api/x/download/cancel")
    def x_download_cancel():
        if x_service is None:
            return _x_unavailable()
        denied = _same_origin_request()
        if denied is not None:
            return denied
        return jsonify(ok=x_service.cancel_download())

    @app.post("/api/login")
    def login():
        data = request.get_json(silent=True) or {}
        username = (data.get("username") or "").strip().lstrip("@")
        password = data.get("password") or ""
        code = (data.get("code") or "").strip()
        try:
            downloader.login(
                username,
                password,
                two_factor_provider=(lambda: code) if code else None,
            )
        except TwoFactorRequired:
            # Kein Fehler: Die Oberfläche blendet daraufhin das Code-Feld ein.
            return jsonify(needs_2fa=True), 401
        except LoginError as exc:
            return jsonify(error=str(exc)), 401
        return jsonify(ok=True, user=downloader.logged_in_user or "")

    @app.post("/api/import-session")
    def import_session():
        data = request.get_json(silent=True) or {}
        browser = (data.get("browser") or "").strip() or None
        try:
            user = downloader.import_browser_session(browser)
        except LoginError as exc:
            return jsonify(error=str(exc)), 401
        return jsonify(ok=True, user=user)

    @app.post("/api/login-sessionid")
    def login_sessionid():
        data = request.get_json(silent=True) or {}
        sessionid = data.get("sessionid") or ""
        try:
            user = downloader.login_with_sessionid(sessionid)
        except LoginError as exc:
            return jsonify(error=str(exc)), 401
        return jsonify(ok=True, user=user)

    @app.post("/api/import-list")
    def import_list():
        """Füllt Gefolgt/Follower aus einer HOCHGELADENEN Datei.

        So erscheinen die Listen sofort, ganz ohne Instagram-Abruf – hilfreich,
        wenn Instagram gerade eine Sicherheitsbestätigung verlangt. Erwartet
        ein Formularfeld ``file`` und ``kind`` = "following" oder "followers".
        Ist die Datei Instagrams komplettes Export-ZIP, werden BEIDE Listen
        automatisch erkannt und in einem Rutsch übernommen.
        """
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            return jsonify(error="Keine Datei ausgewählt."), 400
        raw = upload.read()
        if not raw:
            return jsonify(error="Die Datei ist leer."), 400
        if len(raw) > 200 * 1024 * 1024:
            return jsonify(error="Die Datei ist zu groß (max. 200 MB)."), 400

        # Instagram-Export-ZIP: enthält Gefolgt UND Follower → beides.
        import io as _io
        import zipfile as _zipfile
        if _zipfile.is_zipfile(_io.BytesIO(raw)):
            try:
                both = parse_export_zip(raw)
            except ImportError_ as exc:
                return jsonify(error=str(exc)), 400
            counts = {}
            for zip_kind in ("following", "followers"):
                if both.get(zip_kind):
                    db.replace_social_list(zip_kind, both[zip_kind])
                    counts[zip_kind] = len(both[zip_kind])
            if not counts:
                return jsonify(error="Im ZIP wurden keine Gefolgt-/Follower-"
                                     "Daten gefunden. Beim Instagram-Export "
                                     "das Format JSON wählen."), 400
            return jsonify(ok=True, zip=True,
                           following=counts.get("following", 0),
                           followers=counts.get("followers", 0))

        kind = (request.form.get("kind") or "").strip()
        if kind not in ("following", "followers"):
            return jsonify(error="Bitte „Gefolgt“ oder „Follower“ wählen."), 400
        try:
            accounts = parse_account_file(raw, upload.filename)
        except ImportError_ as exc:
            return jsonify(error=str(exc)), 400
        db.replace_social_list(kind, accounts)
        # Nach dem Import fehlende Profilbilder im Hintergrund nachladen –
        # geht nur, wenn angemeldet; sonst bleiben Platzhalter (unkritisch).
        return jsonify(ok=True, kind=kind, count=len(accounts))

    @app.post("/api/logout")
    def logout():
        downloader.logout()
        return jsonify(ok=True)

    # ------------------------------------------------------------------
    # Abonnements (wem du folgst) + Massen-Download
    # ------------------------------------------------------------------
    bulk = _BulkJob()

    # Profilbild-URLs der Abos: username → CDN-URL (für den Avatar-Proxy).
    avatar_urls: dict[str, str] = {}

    def _social_row_json(row) -> dict:
        """Serialisiert einen Eintrag der zwischengespeicherten Listen."""
        username = row["username"]
        if row["pic_url"]:
            avatar_urls[username] = row["pic_url"]
        last = row["last_post_at"] or ""
        seen = row["seen_post_at"]
        return {
            "username": username,
            "full_name": row["full_name"] or "",
            "avatar": (
                f"/avatar/{username}"
                if (row["pic_url"] or social.avatar_file(username)) else ""
            ),
            "last_post_at": last,
            "checked_at": row["checked_at"] or "",
            # NEU = es gibt einen Beitrag, der jünger ist als das, was der
            # Nutzer zuletzt gesehen hat (Gesehen-Marke gesetzt beim Öffnen
            # des Profils in der App).
            "is_new": bool(last and seen is not None and last > seen),
            "favorite": bool(row["favorite"]),
            "group": row["group_name"] or "",
            "has_story": bool(row["has_story"]),
        }

    def _cached_social(kind: str) -> tuple[list[dict], str]:
        rows = db.get_social_list(kind)
        return [_social_row_json(r) for r in rows], db.social_fetched_at(kind)

    @app.get("/api/overview")
    def api_overview():
        """EIN Sammel-Aufruf beim Öffnen: beide Listen sofort aus der DB.

        ``?since=<version>`` liefert nur dann Daten, wenn sich seit dieser
        Version etwas geändert hat – die Oberfläche rendert sonst nicht neu.
        """
        etag = db.overview_etag()
        if request.args.get("since") == etag:
            return jsonify(version=etag, unchanged=True)
        following_accounts, following_at = _cached_social("following")
        follower_accounts, followers_at = _cached_social("followers")
        return jsonify(
            version=etag,
            logged_in=downloader.is_logged_in,
            user=downloader.logged_in_user or "",
            refresh=social.status(),
            following={
                "fetched_at": following_at,
                "count": len(following_accounts),
                "accounts": following_accounts,
            },
            followers={
                "fetched_at": followers_at,
                "count": len(follower_accounts),
                "accounts": follower_accounts,
            },
        )

    @app.post("/api/social/refresh")
    def social_refresh():
        """Manuelle Auffrischung der Listen (Knopf „Jetzt aktualisieren")."""
        if not downloader.is_logged_in:
            return jsonify(error="Bitte zuerst anmelden."), 401
        if not social.refresh_now():
            return jsonify(error="Die Listen werden gerade aktualisiert."), 409
        return jsonify(ok=True)

    @app.post("/api/seen")
    def mark_seen():
        """Gesehen-Marke setzen: Die NEU-Markierung dieses Kontos erlischt."""
        data = request.get_json(silent=True) or {}
        username = extract_username(str(data.get("username", "")))
        if not username:
            return jsonify(error="Ungültiger Benutzername."), 400
        db.mark_account_seen(username)
        return jsonify(ok=True)

    @app.post("/api/favorite")
    def set_favorite():
        """⭐ Konto anpinnen/lösen (steht dann in den Listen ganz oben)."""
        data = request.get_json(silent=True) or {}
        username = extract_username(str(data.get("username", "")))
        if not username:
            return jsonify(error="Ungültiger Benutzername."), 400
        db.set_favorite(username, bool(data.get("on")))
        return jsonify(ok=True)

    @app.post("/api/group")
    def set_group():
        """Freie Gruppe/Etikett für ein Konto setzen ("" entfernt sie)."""
        data = request.get_json(silent=True) or {}
        username = extract_username(str(data.get("username", "")))
        if not username:
            return jsonify(error="Ungültiger Benutzername."), 400
        db.set_group(username, str(data.get("group", "")))
        return jsonify(ok=True)

    @app.get("/api/social/events")
    def social_events():
        """Verlauf der Listen: Wer kam dazu, wer ist weg (aus den Snapshots)."""
        rows = db.social_events(200)
        return jsonify(events=[{
            "kind": r["kind"],
            "username": r["username"],
            "event": r["event"],
            "happened_at": r["happened_at"],
        } for r in rows])

    @app.get("/api/following")
    def following():
        # Standard: der lokale Zwischenspeicher – sofort da, kein
        # Instagram-Abruf. Mit ?fresh=1 wird bewusst neu von Instagram
        # geholt (z. B. „Abo-Liste neu laden“, nachdem man jemandem neu folgt).
        fresh = request.args.get("fresh") == "1"
        if not fresh:
            accounts, fetched_at = _cached_social("following")
            if accounts:
                return jsonify(count=len(accounts), accounts=accounts,
                               fetched_at=fetched_at, cached=True)
        # Live holen und dabei gleich den Zwischenspeicher füllen.
        try:
            details = downloader.following_details()
        except LoginError as exc:
            return jsonify(error=str(exc)), 401
        except DownloaderError as exc:
            return jsonify(error=str(exc)), 502
        db.replace_social_list("following", details)
        accounts = []
        for item in details:
            if item.get("pic"):
                avatar_urls[item["username"]] = item["pic"]
            accounts.append({
                "username": item["username"],
                "full_name": item.get("full_name", ""),
                "avatar": f"/avatar/{item['username']}" if item.get("pic") else "",
            })
        return jsonify(count=len(accounts), accounts=accounts, fresh=True)

    def _avatar_url(item: dict) -> str:
        """Merkt sich die Profilbild-URL und liefert den Proxy-Pfad."""
        if item.get("pic"):
            avatar_urls[item["username"]] = item["pic"]
            return f"/avatar/{item['username']}"
        return ""

    @app.get("/api/me")
    def api_me():
        try:
            info = downloader.me()
        except LoginError as exc:
            return jsonify(error=str(exc)), 401
        except DownloaderError as exc:
            return jsonify(error=str(exc)), 502
        info = dict(info)
        info["avatar"] = _avatar_url(info)
        info.pop("pic", None)
        return jsonify(info)

    @app.get("/api/profile/<username>")
    def api_profile(username: str):
        uname = extract_username(username)
        if not uname:
            return jsonify(error="Ungültiger Benutzername."), 400
        try:
            info = downloader.profile_info(uname)
        except ProfileNotFound as exc:
            return jsonify(error=str(exc)), 404
        except ProfileIsPrivate as exc:
            return jsonify(error=str(exc)), 403
        except DownloaderError as exc:
            return jsonify(error=str(exc)), 502
        info = dict(info)
        info["avatar"] = _avatar_url(info)
        info.pop("pic", None)
        return jsonify(info)

    @app.get("/api/account-files/<username>")
    def account_files(username: str):
        """Wie viele Dateien von diesem Konto bereits lokal liegen – nach Typ
        getrennt (Fotos/Videos/Story/Highlights), für die Profil-Anzeige.
        Zählt direkt die Dateien auf der Platte (überlebt App-Neuinstallation)."""
        uname = extract_username(username)
        if not uname:
            return jsonify(error="Ungültiger Benutzername."), 400
        # Ordnernamen wie im Downloader (_PHOTO_SUBDIR usw.).
        counts = {"Fotos": 0, "Videos": 0, "Story": 0, "Highlights": 0}
        total_bytes = 0
        try:
            root = settings.download_dir / uname
            for sub in counts:
                d = root / sub
                if d.is_dir():
                    for f in d.rglob("*"):
                        if f.is_file():
                            counts[sub] += 1
                            try:
                                total_bytes += f.stat().st_size
                            except OSError:
                                pass
        except Exception:
            pass
        return jsonify(username=uname, counts=counts,
                       total=sum(counts.values()), bytes=total_bytes)

    @app.get("/api/followers")
    def api_followers():
        # Standard: der lokale Zwischenspeicher; ?fresh=1 erzwingt Live-Abruf.
        fresh = request.args.get("fresh") == "1"
        if not fresh:
            accounts, fetched_at = _cached_social("followers")
            if accounts:
                return jsonify(count=len(accounts), accounts=accounts,
                               fetched_at=fetched_at, cached=True)
        try:
            details = downloader.followers_details()
        except LoginError as exc:
            return jsonify(error=str(exc)), 401
        except DownloaderError as exc:
            return jsonify(error=str(exc)), 502
        db.replace_social_list("followers", details)
        return jsonify(count=len(details), fresh=True, accounts=[{
            "username": d["username"], "full_name": d.get("full_name", ""),
            "avatar": _avatar_url(d),
        } for d in details])

    @app.get("/api/search")
    def api_search():
        query = request.args.get("q", "")
        try:
            results = downloader.search_profiles(query, limit=12)
        except DownloaderError as exc:
            return jsonify(error=str(exc)), 502
        # Für die Knöpfe am Treffer: steht das Konto schon in „Gefolgt"
        # (lokale Liste) bzw. ist es angepinnt (⭐)?
        try:
            following_set = {a["username"] for a in _cached_social("following")[0]}
        except Exception:
            following_set = set()
        return jsonify(results=[{
            "username": d["username"], "full_name": d.get("full_name", ""),
            "is_verified": d.get("is_verified", False), "avatar": _avatar_url(d),
            "following": d["username"] in following_set,
            "favorite": db.is_favorite(d["username"]),
        } for d in results])

    @app.post("/api/following/add")
    def following_add():
        """Ein Konto zur lokalen „Gefolgt"-Liste hinzufügen (z. B. aus der
        Suche). Kein echter Instagram-Follow – nur die App-eigene Liste, damit
        das Konto im Feed erscheint und ladbar ist."""
        data = request.get_json(silent=True) or {}
        username = extract_username(str(data.get("username", "")))
        if not username:
            return jsonify(error="Ungültiger Benutzername."), 400
        db.add_social_account("following", {
            "username": username,
            "full_name": str(data.get("full_name", "")),
            "pic": avatar_urls.get(username, ""),
        })
        return jsonify(ok=True)

    @app.post("/api/following/remove")
    def following_remove():
        """Ein Konto aus der lokalen „Gefolgt"-Liste entfernen."""
        data = request.get_json(silent=True) or {}
        username = extract_username(str(data.get("username", "")))
        if not username:
            return jsonify(error="Ungültiger Benutzername."), 400
        db.remove_social_account("following", username)
        return jsonify(ok=True)

    @app.get("/avatar/<username>")
    def avatar(username: str):
        """Profilbild: zuerst aus dem lokalen Cache (sofort, dauerhaft gültig).

        Fällt auf die zuletzt gemerkte CDN-URL zurück und legt das Bild
        dabei gleich im Cache ab – Instagram-Bild-URLs laufen nach einigen
        Stunden ab, die lokale Kopie nicht.
        """
        local = social.avatar_file(username)
        if local is None:
            url = avatar_urls.get(username)
            if url:
                local = social.store_avatar(username, url)
        if local is None:
            return Response(status=404)
        try:
            resp = send_file(local, mimetype="image/jpeg")
        except OSError:
            return Response(status=404)
        resp.headers["Cache-Control"] = "private, max-age=21600"
        return resp

    def _type_matches(post_type: str, types: str) -> bool:
        """Medientyp-Filter: „photos“ = Bild/Album, „videos“ = Video/Reel."""
        if types == "photos":
            return post_type in ("Bild", "Album")
        if types == "videos":
            # „Video/Reel“ ist der Wert, den _classify_post für ein normales
            # Video vergibt (siehe downloader.py); „Video“ allein kommt dort
            # nie vor. Ohne diesen Eintrag ließ „nur Videos“ fast alle Videos
            # aus und lud nur Reels.
            return post_type in ("Video", "Video/Reel", "Reel")
        return True

    # ------------------------------------------------------------------
    # Fortsetzbarer Massen-Download: Der laufende Auftrag wird in der
    # Datenbank festgehalten (Konto-Liste + wie weit er gekommen ist).
    # Bricht der Nutzer bei 600 Konten nach 200 ab, kann er beim nächsten
    # Mal bei 201 weitermachen statt wieder von vorne anzufangen.
    # ------------------------------------------------------------------
    _BULK_STATE_KEY = "bulk_job_state"

    def _save_bulk_state(usernames: list[str], done: int, per_account: int,
                         since: str, types: str) -> None:
        try:
            db.set_setting(_BULK_STATE_KEY, json.dumps({
                "usernames": usernames, "done": done,
                "count": per_account, "since": since, "types": types,
            }))
        except Exception:
            logger.debug("Massen-Download-Stand nicht speicherbar.", exc_info=True)

    def _load_bulk_state() -> dict:
        try:
            raw = db.get_setting(_BULK_STATE_KEY, "") or ""
            data = json.loads(raw) if raw else {}
            if isinstance(data, dict) and isinstance(data.get("usernames"), list):
                return data
        except Exception:
            pass
        return {}

    def start_bulk_job(usernames: list[str], per_account: int, since: str = "",
                       also_stories: bool = False, types: str = "all") -> bool:
        """Startet den Massen-Download im Hintergrund (Endpunkt & Zeitplan).

        ``also_stories`` lädt je Konto zusätzlich die aktuellen Stories.
        ``types`` filtert die Medienart: "all" (Beiträge), "photos"
        (Bild/Album), "videos" (Video/Reel), "stories" (NUR Stories),
        "highlights" (NUR Highlights), "everything" (Beiträge + Stories +
        Highlights) oder "complete" (wie everything, aber ALLE Beiträge des
        Kontos statt nur der neuesten – das Konto gilt danach als erledigt).
        Rückgabe: False, wenn bereits ein Download läuft.
        """
        want_stories = also_stories or types in ("stories", "everything",
                                                 "complete")
        want_highlights = types in ("highlights", "everything", "complete")
        # „complete" heißt WIRKLICH alles: kein Beitrags-Limit.
        if types == "complete":
            per_account = _COMPLETE_COUNT
        # Atomar starten: verhindert doppelte Worker bei Zeitplan+Klick.
        if not bulk.try_start(total=len(usernames)):
            return False
        # Auftrag merken, damit ein abgebrochener Lauf später GENAU dort
        # weitergehen kann, wo er aufgehört hat (statt bei 600 Konten wieder
        # von vorn). Wird nach jedem fertigen Konto aktualisiert.
        _save_bulk_state(usernames, 0, per_account, since, types)
        # Warteschlange für die Live-Ansicht („Was wird gerade geladen?").
        bulk.set_items(usernames)

        def worker():
            downloaded_total = 0
            try:
                for index, username in enumerate(usernames):
                    if bulk.cancel:
                        break
                    bulk.update(current=username)
                    bulk.mark_item(index, state="active")
                    acct_downloaded = 0
                    if types in ("stories", "highlights"):
                        posts = []   # nur Stories/Highlights – Beiträge auslassen
                    else:
                        try:
                            posts = downloader.list_recent_posts(username, per_account)
                        except DownloaderError as exc:
                            logger.warning("Massen-Download @%s übersprungen: %s",
                                           username, exc)
                            bulk.mark_item(index, state="done", n=0)
                            bulk.update(done=index + 1)
                            continue
                    for post in posts:
                        if bulk.cancel:
                            break
                        # Medientyp-Filter (nur Fotos / nur Videos).
                        if not _type_matches(post.post_type, types):
                            continue
                        # Datumsfilter: alte Beiträge überspringen.
                        if since and (post.posted_at or "")[:10] < since:
                            continue
                        # Dedup: bereits heruntergeladene NICHT erneut laden.
                        if db.is_downloaded(post.shortcode, username):
                            continue
                        db.add_post(post.shortcode, username, post.post_type,
                                    post.posted_at)
                        try:
                            if downloader.download_post(post):
                                db.mark_downloaded(post.shortcode, username)
                                downloaded_total += 1
                                acct_downloaded += 1
                                bulk.update(downloaded=downloaded_total)
                                bulk.mark_item(index, n=acct_downloaded)
                        except DownloaderError as exc:
                            logger.warning("Beitrag %s nicht geladen: %s",
                                           post.shortcode, exc)
                    # Optional: aktuelle Stories und/oder Highlights mitladen
                    # (beide werden vom download_story_item typgetrennt in
                    # Fotos/Videos einsortiert).
                    for _kind, _want in (("stories", want_stories),
                                         ("highlights", want_highlights)):
                        if not _want or bulk.cancel:
                            continue
                        try:
                            for item in downloader.list_story_items(username, _kind):
                                if bulk.cancel:
                                    break
                                if db.is_downloaded(item.shortcode, username):
                                    continue
                                db.add_post(item.shortcode, username,
                                            item.post_type, item.posted_at)
                                if downloader.download_story_item(item):
                                    db.mark_downloaded(item.shortcode, username)
                                    downloaded_total += 1
                                    acct_downloaded += 1
                                    bulk.update(downloaded=downloaded_total)
                                    bulk.mark_item(index, n=acct_downloaded)
                        except DownloaderError as exc:
                            logger.warning("%s @%s übersprungen: %s",
                                           _kind, username, exc)
                    # Profilbild des Kontos mitsichern (einmalig; nutzt die
                    # beim Beitrags-Abruf gemerkte CDN-URL, kein Extra-Abruf).
                    try:
                        pic_fn = getattr(downloader, "download_profile_pic", None)
                        if pic_fn is not None:
                            pic_fn(username)
                    except Exception:
                        logger.debug("Profilbild @%s nicht gespeichert.", username,
                                     exc_info=True)
                    bulk.mark_item(index, state="done", n=acct_downloaded)
                    # „Komplett"-Lauf: Konto als ERLEDIGT vermerken (nur wenn
                    # nicht abgebrochen wurde – sonst wäre es nur halb geladen).
                    if types == "complete" and not bulk.cancel:
                        db.mark_account_complete(username)
                    bulk.update(done=index + 1)
                    # Fortschritt sichern: Nach einem Abbruch kann hier
                    # weitergemacht werden (siehe /api/download-following/resume).
                    _save_bulk_state(usernames, index + 1, per_account, since, types)
                    # Rücksichtsvolle Pause zwischen den Konten (mit Jitter,
                    # damit kein maschinenhaft exakter Takt entsteht).
                    if index < len(usernames) - 1 and not bulk.cancel:
                        time.sleep(_BULK_DELAY + random.uniform(0.0, 2.0))
            except Exception:
                logger.exception("Massen-Download abgebrochen (Fehler).")
                bulk.update(error="Unerwarteter Fehler – siehe logs/web.log.")
            finally:
                bulk.update(running=False, finished=True)
                logger.info("Massen-Download beendet: %d Beitrag/Beiträge "
                            "gespeichert.", downloaded_total)

        thread = threading.Thread(target=worker, name="bulk-download", daemon=True)
        bulk.thread = thread
        thread.start()
        return True

    @app.post("/api/download-following")
    def download_following():
        if bulk.snapshot()["running"]:
            return jsonify(error="Es läuft bereits ein Massen-Download."), 409
        data = request.get_json(silent=True) or {}
        try:
            per_account = max(1, min(_MAX_COUNT, int(data.get("count", 3))))
        except (TypeError, ValueError):
            per_account = 3
        # Optionaler Datumsfilter: nur Beiträge ab diesem Tag (YYYY-MM-DD).
        since = (data.get("since") or "").strip()[:10]

        # Ausgewählte Konten (falls angegeben), sonst alle Abos.
        selected = data.get("usernames")
        if isinstance(selected, list) and selected:
            usernames = [str(u).strip().lstrip("@").lower()
                         for u in selected if str(u).strip()]
        else:
            try:
                usernames = downloader.following_usernames()
            except LoginError as exc:
                return jsonify(error=str(exc)), 401
            except DownloaderError as exc:
                return jsonify(error=str(exc)), 502
        if not usernames:
            return jsonify(error="Keine Konten ausgewählt."), 400

        # Medientyp-Auswahl: alles / nur Fotos / nur Videos+Reels / nur Stories
        # / everything (Beiträge + Stories + Highlights).
        types = (data.get("types") or "all").strip()
        if types not in ("all", "photos", "videos", "stories", "highlights",
                         "everything", "complete"):
            types = "all"

        if not start_bulk_job(usernames, per_account, since, types=types):
            return jsonify(error="Es läuft bereits ein Massen-Download."), 409
        return jsonify(ok=True, total=len(usernames), types=types)

    @app.get("/api/accounts-complete")
    def accounts_complete():
        """Die „Erledigt"-Liste: Konten, von denen ALLES geladen wurde."""
        done = db.completed_accounts()
        try:
            total = len(_cached_social("following")[0])
        except Exception:
            total = 0
        return jsonify(accounts=done, count=len(done), following_total=total)

    @app.post("/api/accounts-complete/reset")
    def accounts_complete_reset():
        """Erledigt-Vermerk entfernen – das Konto wird dann erneut geladen."""
        data = request.get_json(silent=True) or {}
        username = extract_username(str(data.get("username", "")))
        if not username:
            return jsonify(error="Ungültiger Benutzername."), 400
        db.clear_account_complete(username)
        return jsonify(ok=True)

    @app.post("/api/download-all-complete")
    def download_all_complete():
        """ALLE Abo-Konten wirklich komplett laden (Beiträge + Stories +
        Highlights). Bereits erledigte Konten werden übersprungen, sofern
        nicht ``include_done`` gesetzt ist."""
        if bulk.snapshot()["running"]:
            return jsonify(error="Es läuft bereits ein Massen-Download."), 409
        data = request.get_json(silent=True) or {}
        selected = data.get("usernames")
        if isinstance(selected, list) and selected:
            usernames = [str(u).strip().lstrip("@").lower()
                         for u in selected if str(u).strip()]
        else:
            accounts, _ = _cached_social("following")
            usernames = [a["username"] for a in accounts]
            if not usernames:
                try:
                    usernames = downloader.following_usernames()
                except LoginError as exc:
                    return jsonify(error=str(exc)), 401
                except DownloaderError as exc:
                    return jsonify(error=str(exc)), 502
        skipped = 0
        if not data.get("include_done"):
            before = len(usernames)
            usernames = [u for u in usernames if not db.is_account_complete(u)]
            skipped = before - len(usernames)
        if not usernames:
            return jsonify(ok=True, total=0, skipped=skipped,
                           message="Alle Konten sind bereits komplett geladen.")
        if not start_bulk_job(usernames, _COMPLETE_COUNT, "", types="complete"):
            return jsonify(error="Es läuft bereits ein Massen-Download."), 409
        return jsonify(ok=True, total=len(usernames), skipped=skipped,
                       types="complete")

    @app.post("/api/download-stories")
    def download_stories():
        if bulk.snapshot()["running"]:
            return jsonify(error="Es läuft bereits ein Download."), 409
        data = request.get_json(silent=True) or {}
        username = extract_username(data.get("username", ""))
        kind = data.get("kind", "stories")
        if kind not in ("stories", "highlights"):
            kind = "stories"
        if not username:
            return jsonify(error="Bitte einen gültigen Benutzernamen eingeben."), 400
        try:
            items = downloader.list_story_items(username, kind)
        except LoginError as exc:
            return jsonify(error=str(exc)), 401
        except DownloaderError as exc:
            return jsonify(error=str(exc)), 502
        if not items:
            return jsonify(ok=True, total=0)

        if not bulk.try_start(total=len(items), current=username):
            return jsonify(error="Es läuft bereits ein Download."), 409

        def worker():
            downloaded = 0
            try:
                for index, item in enumerate(items):
                    if bulk.cancel:
                        break
                    if db.is_downloaded(item.shortcode, username):
                        bulk.update(done=index + 1)
                        continue
                    db.add_post(item.shortcode, username, item.post_type,
                                item.posted_at)
                    try:
                        if downloader.download_story_item(item):
                            db.mark_downloaded(item.shortcode, username)
                            downloaded += 1
                            bulk.update(downloaded=downloaded)
                    except DownloaderError as exc:
                        logger.warning("Story %s nicht geladen: %s",
                                       item.shortcode, exc)
                    bulk.update(done=index + 1)
            except Exception:
                logger.exception("Story-Download abgebrochen (Fehler).")
                bulk.update(error="Unerwarteter Fehler – siehe logs/web.log.")
            finally:
                bulk.update(running=False, finished=True)
                logger.info("Story-Download @%s beendet: %d Element(e).",
                            username, downloaded)

        thread = threading.Thread(target=worker, name="story-download", daemon=True)
        bulk.thread = thread
        thread.start()
        return jsonify(ok=True, total=len(items))

    @app.get("/api/download-following/status")
    def download_following_status():
        return jsonify(bulk.snapshot())

    @app.get("/api/download-following/resume-info")
    def download_following_resume_info():
        """Gibt es einen unterbrochenen Auftrag, den man fortsetzen kann?"""
        state = _load_bulk_state()
        total = len(state.get("usernames", []))
        done = int(state.get("done", 0) or 0)
        return jsonify(
            available=bool(total and done < total),
            total=total, done=done, remaining=max(0, total - done),
            types=state.get("types", "all"),
        )

    @app.post("/api/download-following/resume")
    def download_following_resume():
        """Setzt den zuletzt unterbrochenen Massen-Download dort fort, wo er
        aufgehört hat (die bereits erledigten Konten werden übersprungen)."""
        if bulk.snapshot()["running"]:
            return jsonify(error="Es läuft bereits ein Download."), 409
        state = _load_bulk_state()
        names = state.get("usernames", [])
        done = int(state.get("done", 0) or 0)
        rest = names[done:]
        if not rest:
            return jsonify(error="Es gibt nichts zum Fortsetzen."), 400
        if not start_bulk_job(rest, int(state.get("count", 3) or 3),
                              str(state.get("since", "") or ""),
                              types=str(state.get("types", "all") or "all")):
            return jsonify(error="Es läuft bereits ein Download."), 409
        return jsonify(ok=True, total=len(rest), resumed_from=done)

    @app.post("/api/download-following/cancel")
    def download_following_cancel():
        bulk.update(cancel=True)
        return jsonify(ok=True)

    # ------------------------------------------------------------------
    # Abo-Feed (beim Öffnen automatisch: neueste Beiträge deiner Abos)
    # ------------------------------------------------------------------
    feed = _FeedJob()

    @app.get("/api/feed/settings")
    def feed_get_settings():
        return jsonify(profiles=settings.feed_profiles, per=settings.feed_per)

    @app.post("/api/feed/start")
    def feed_start():
        if not downloader.is_logged_in:
            return jsonify(error="Bitte zuerst anmelden, um den Abo-Feed zu "
                                 "sehen."), 401
        if feed.snapshot(include_items=False)["running"]:
            return jsonify(error="Der Feed wird bereits geladen."), 409
        data = request.get_json(silent=True) or {}
        try:
            profiles = max(0, int(data.get("profiles", settings.feed_profiles)))
        except (TypeError, ValueError):
            profiles = settings.feed_profiles
        try:
            per = max(1, min(_MAX_COUNT, int(data.get("per", settings.feed_per))))
        except (TypeError, ValueError):
            per = settings.feed_per
        # Seiten-Versatz für Endlos-Scroll (welche Profile-Seite laden).
        try:
            offset = max(0, int(data.get("offset", 0)))
        except (TypeError, ValueError):
            offset = 0
        # Wahl merken (gilt beim nächsten Öffnen); Versatz wird nicht gemerkt.
        settings.feed_profiles = profiles
        settings.feed_per = per

        try:
            all_usernames = downloader.following_usernames()
        except LoginError as exc:
            return jsonify(error=str(exc)), 401
        except DownloaderError as exc:
            return jsonify(error=str(exc)), 502
        followees_total = len(all_usernames)
        # NEU gefolgte Konten nach vorne holen: Instagram liefert die
        # Abo-Liste NICHT chronologisch, und der Feed lädt standardmäßig nur
        # die ersten paar Profile. Wer neu gefolgt wurde (steht in der Live-
        # Liste, aber noch nicht im lokalen Zwischenspeicher), fiele sonst aus
        # dem Feed heraus und man sähe seine Beiträge nie. Darum: unbekannte
        # (= neu gefolgte) Konten zuerst.
        try:
            known = {a["username"] for a in _cached_social("following")[0]}
        except Exception:
            known = set()
        if known:
            fresh_first = [u for u in all_usernames if u not in known]
            if fresh_first:
                rest = [u for u in all_usernames if u in known]
                all_usernames = fresh_first + rest
        if profiles > 0:
            usernames = all_usernames[offset:offset + profiles]
        else:
            usernames = all_usernames[offset:]
        if not usernames:
            return jsonify(ok=True, total=0, followees_total=followees_total)

        feed.reset()
        feed.update(running=True, total=len(usernames))

        def worker():
            try:
                for index, username in enumerate(usernames):
                    if feed.cancel:
                        break
                    feed.update(current=username)
                    try:
                        posts = downloader.list_recent_posts(username, per)
                    except DownloaderError as exc:
                        logger.warning("Feed @%s übersprungen: %s", username, exc)
                        feed.update(done=index + 1)
                        continue
                    remember(posts)
                    items = [post_json(p, username) for p in posts]
                    feed.add_items(items)
                    # In den Sofort-Feed übernehmen: Beim nächsten Öffnen
                    # ist der Feed ohne Wartezeit da (wie bei Instagram).
                    _save_feed_items(items)
                    feed.update(done=index + 1)
                    if index < len(usernames) - 1 and not feed.cancel:
                        time.sleep(_FEED_DELAY + random.uniform(0.0, 1.0))
            except Exception:
                logger.exception("Feed-Aufbau fehlgeschlagen.")
                feed.update(error="Unerwarteter Fehler – siehe logs/web.log.")
            finally:
                feed.update(running=False, finished=True)

        thread = threading.Thread(target=worker, name="feed-build", daemon=True)
        feed.thread = thread
        thread.start()
        return jsonify(ok=True, total=len(usernames),
                       followees_total=followees_total,
                       next_offset=offset + len(usernames))

    @app.get("/api/feed/cached")
    def feed_cached():
        """Der Sofort-Feed: die zuletzt geladenen Beiträge aus dem lokalen
        Speicher – erscheint beim Öffnen ohne jeden Instagram-Abruf."""
        return jsonify(_load_cached_feed())

    @app.get("/api/feed/status")
    def feed_status():
        return jsonify(feed.snapshot())

    @app.post("/api/feed/cancel")
    def feed_cancel():
        feed.update(cancel=True)
        return jsonify(ok=True)

    # ------------------------------------------------------------------
    # Beiträge ansehen
    # ------------------------------------------------------------------
    @app.get("/api/posts")
    def posts():
        username = extract_username(request.args.get("username", ""))
        if not username:
            return jsonify(error="Bitte einen gültigen Benutzernamen oder "
                                 "Profil-Link eingeben."), 400
        try:
            count = max(1, min(_MAX_COUNT, int(request.args.get("count", 12))))
        except ValueError:
            count = 12

        try:
            items = downloader.list_recent_posts(username, count)
        except ProfileIsPrivate as exc:
            return jsonify(error=str(exc)), 403
        except ProfileNotFound as exc:
            return jsonify(error=str(exc)), 404
        except TemporaryError as exc:
            return jsonify(error=str(exc)), 502
        except DownloaderError as exc:  # Sammelbecken
            return jsonify(error=str(exc)), 502

        remember(items)
        return jsonify(
            username=username,
            posts=[post_json(p, username) for p in items],
        )

    @app.get("/media/<shortcode>")
    def media(shortcode: str):
        """Streamt das Medium des Beitrags an das anfragende Gerät.

        So landet ein Download auf dem Handy tatsächlich auf dem Handy
        (Content-Disposition: attachment), statt im PC-Ordner. Bei einem
        Album wird über ?i=<n> das n-te Element gewählt (Standard: 0).
        """
        post = lookup_post(shortcode)
        if post is None:
            return Response("Beitrag nicht bekannt – bitte neu laden.",
                            status=404)
        try:
            items = downloader.media_urls(post)
        except DownloaderError as exc:
            return Response(str(exc), status=502)
        if not items:
            return Response("Kein herunterladbares Medium gefunden.", status=404)
        try:
            index = max(0, min(len(items) - 1, int(request.args.get("i", 0))))
        except ValueError:
            index = 0
        url, filename = items[index]
        try:
            upstream = _requests.get(
                url, timeout=60, stream=True,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            upstream.raise_for_status()
        except Exception:
            logger.debug("Medium %s nicht ladbar.", shortcode, exc_info=True)
            return Response("Medium konnte nicht geladen werden.", status=502)
        # ?inline=1 → im Browser anzeigen (Feed/Lightbox); sonst als Download
        # (Content-Disposition: attachment), damit "⤓ Gerät" wirklich speichert.
        headers = {"Cache-Control": "private, max-age=3600"}
        if request.args.get("inline") != "1":
            headers["Content-Disposition"] = (
                f"attachment; filename=\"{filename}\"; "
                f"filename*=UTF-8''{quote(filename)}"
            )

        def _stream():
            # Die Upstream-Verbindung IMMER schließen – auch wenn der Client
            # den Download mittendrin abbricht (dann beendet Werkzeug den
            # Generator, das finally läuft). Verhindert offene Sockets/FDs.
            try:
                for chunk in upstream.iter_content(chunk_size=65536):
                    yield chunk
            finally:
                upstream.close()

        return Response(
            _stream(),
            mimetype=upstream.headers.get("Content-Type", "application/octet-stream"),
            headers=headers,
        )

    @app.get("/api/comments/<shortcode>")
    def comments(shortcode: str):
        post = lookup_post(shortcode)
        if post is None:
            return jsonify(error="Beitrag nicht bekannt – bitte neu laden."), 404
        try:
            items = downloader.get_comments(post, limit=15)
        except LoginError as exc:
            return jsonify(error=str(exc)), 401
        except DownloaderError as exc:
            return jsonify(error=str(exc)), 502
        return jsonify(comments=items)

    @app.get("/api/hashtag/<tag>")
    def hashtag(tag: str):
        try:
            count = max(1, min(_MAX_COUNT, int(request.args.get("count", 24))))
        except (TypeError, ValueError):
            count = 24
        try:
            items = downloader.hashtag_posts(tag, limit=count)
        except ProfileNotFound as exc:
            return jsonify(error=str(exc)), 404
        except TemporaryError as exc:
            return jsonify(error=str(exc)), 502
        except DownloaderError as exc:
            return jsonify(error=str(exc)), 502
        remember(items)
        return jsonify(tag=tag.strip().lstrip("#").lower(),
                       posts=[post_json(p, p.username) for p in items])

    @app.get("/api/album/<shortcode>")
    def album(shortcode: str):
        """Liefert die einzelnen Medien eines Albums (für das Karussell)."""
        post = lookup_post(shortcode)
        if post is None:
            return jsonify(error="Beitrag nicht bekannt – bitte neu laden."), 404
        try:
            items = downloader.media_urls(post)
        except DownloaderError as exc:
            return jsonify(error=str(exc)), 502
        return jsonify(items=[
            {
                "i": index,
                "is_video": filename.lower().endswith(".mp4"),
                "media": f"/media/{shortcode}?i={index}&inline=1",
                "download": f"/media/{shortcode}?i={index}",
            }
            for index, (url, filename) in enumerate(items)
        ])

    @app.get("/api/stories")
    def stories_list():
        """Listet die aktuellen Story-Elemente eines Kontos (Story-Ansicht)."""
        if not downloader.is_logged_in:
            return jsonify(error="Für Stories ist eine Anmeldung nötig."), 401
        username = extract_username(request.args.get("username", ""))
        if not username:
            return jsonify(error="Bitte einen gültigen Benutzernamen eingeben."), 400
        try:
            items = downloader.list_story_items(username, "stories")
        except LoginError as exc:
            return jsonify(error=str(exc)), 401
        except DownloaderError as exc:
            return jsonify(error=str(exc)), 502
        remember(items)
        return jsonify(username=username, items=[
            {
                "shortcode": p.shortcode,
                "is_video": ("Video" in (p.post_type or "")),
                "posted_at": p.posted_at,
                "media": f"/media/{p.shortcode}?inline=1",
                "download": f"/media/{p.shortcode}",
                "thumb": f"/thumb/{p.shortcode}" if p.thumbnail_url else "",
            }
            for p in items
        ])

    # Vorschaubilder dauerhaft auf der Platte behalten: So zeigt der
    # Sofort-Feed seine Bilder auch nach einem Neustart – ohne Internet
    # und obwohl Instagram-CDN-Links nach einiger Zeit ablaufen.
    _thumb_dir = db.path.parent / "cache" / "thumbs"

    @app.get("/thumb/<shortcode>")
    def thumb(shortcode: str):
        """Vorschaubild-Proxy mit Disk-Cache.

        Lädt das Bild serverseitig von der beim Abruf gemerkten CDN-URL
        (KEINE vom Client wählbare URL – schützt vor Missbrauch als
        offener Proxy), legt es lokal ab und reicht es durch. Ist es
        schon lokal vorhanden, kommt es direkt von der Platte.
        """
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", shortcode):
            return Response(status=404)
        disk = _thumb_dir / f"{shortcode}.jpg"
        if disk.is_file():
            resp = send_file(disk, mimetype="image/jpeg")
            resp.headers["Cache-Control"] = "private, max-age=86400"
            return resp
        # Für ein frisches Vorschaubild wird die (kurzlebige) CDN-URL des
        # gemerkten Beitrags gebraucht – die steht nur im In-Memory-Cache.
        # Nach einem Neustart übernimmt der Disk-Cache oben (persistente Kopie).
        post = cache_get(shortcode)
        if post is None or not post.thumbnail_url:
            return Response(status=404)
        try:
            upstream = _requests.get(
                post.thumbnail_url,
                timeout=20,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            upstream.raise_for_status()
        except Exception:
            logger.debug("Vorschaubild %s nicht ladbar.", shortcode, exc_info=True)
            return Response(status=502)
        try:
            _thumb_dir.mkdir(parents=True, exist_ok=True)
            disk.write_bytes(upstream.content)
        except OSError:
            pass  # Cache ist nur Komfort – Anzeige klappt trotzdem
        return Response(
            upstream.content,
            mimetype=upstream.headers.get("Content-Type", "image/jpeg"),
            # Vorschaubilder eines Beitrags ändern sich nicht → der Browser
            # darf sie lange behalten (weniger Abrufe, schnelleres Scrollen).
            headers={"Cache-Control": "private, max-age=86400"},
        )

    # ------------------------------------------------------------------
    # Herunterladen
    # ------------------------------------------------------------------
    @app.post("/api/download")
    def download():
        data = request.get_json(silent=True) or {}
        shortcode = (data.get("shortcode") or "").strip()
        # Konto optional mitliefern: So funktioniert der Download auch nach
        # einem Neustart (leerer Cache), wenn die Oberfläche den Beitrag noch
        # aus dem Sofort-Feed kennt.
        username = extract_username(str(data.get("username", "")))
        post = lookup_post(shortcode)
        if post is None and username:
            post = PostInfo(
                shortcode=shortcode, username=username, post_type="",
                posted_at="", url=f"https://www.instagram.com/p/{shortcode}/",
            )
        if post is None:
            return jsonify(error="Beitrag nicht (mehr) bekannt – bitte die "
                                 "Beiträge neu laden."), 404
        try:
            ok = downloader.download_post(post)
        except DownloaderError as exc:
            return jsonify(error=str(exc)), 502
        if ok:
            # Registrieren + als heruntergeladen markieren, damit auch die
            # Desktop-App den Beitrag nicht erneut lädt.
            db.add_post(post.shortcode, post.username, post.post_type, post.posted_at)
            db.mark_downloaded(post.shortcode, post.username)
        return jsonify(ok=bool(ok))

    # ------------------------------------------------------------------
    # „Instagram-Link speichern": ein Beitrag/Reel wird allein über seinen
    # Link heruntergeladen – angesteuert vom Laptop-Lesezeichen (Bookmarklet)
    # UND vom Handy-Teilen-Ziel (PWA share_target). Beide rufen GET /save auf.
    # ------------------------------------------------------------------
    def _save_result(ok: bool, heading: str, detail: str,
                     status: int = 200) -> Response:
        color = "#2ecc71" if ok else "#e74c3c"
        icon = "✓" if ok else "✕"
        html = (
            '<!doctype html><html lang="de"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            "<title>Speichern – Instagram Monitor</title><style>"
            "body{font-family:system-ui,-apple-system,Arial,sans-serif;"
            "background:#000;color:#fff;margin:0;min-height:100vh;display:flex;"
            "align-items:center;justify-content:center;text-align:center}"
            ".box{padding:26px 20px;max-width:440px}"
            f".ic{{font-size:54px;line-height:1;color:{color}}}"
            ".h{font-size:20px;font-weight:700;margin:12px 0 6px}"
            ".d{color:#bbb;font-size:14px;line-height:1.45}"
            ".d a{color:#5b8cff}</style></head><body><div class=\"box\">"
            f'<div class="ic">{icon}</div><div class="h">{_escape(heading)}</div>'
            f'<div class="d">{_escape(detail)}</div>'
            '<div class="d" style="margin-top:14px">'
            '<a href="/">Instagram Monitor öffnen</a></div>'
            "</div></body></html>"
        )
        return Response(html, status=status,
                        mimetype="text/html; charset=utf-8")

    @app.get("/save")
    def save_link():
        # Geteilter Text/Link kann in mehreren Feldern ankommen (Bookmarklet:
        # ?u=…; Android-Teilen: text/url/title). Alle nach einem Beitragslink
        # absuchen.
        raw = " ".join(
            request.args.get(k, "") for k in ("u", "url", "text", "title")
        )
        shortcode = extract_shortcode(raw)
        if not shortcode:
            return _save_result(
                False, "Kein Beitragslink erkannt",
                "Teile einen Beitrag oder ein Reel (Link mit /p/ oder /reel/) "
                "bzw. nutze das Lesezeichen auf einer Beitragsseite. Reine "
                "Stories haben keinen speicherbaren Link.", status=400)
        try:
            result = downloader.download_by_shortcode(shortcode)
        except ProfileIsPrivate as exc:
            return _save_result(False, "Kein Zugriff", str(exc), status=403)
        except ProfileNotFound as exc:
            return _save_result(False, "Nicht gefunden", str(exc), status=404)
        except LoginError as exc:
            return _save_result(False, "Anmeldung nötig", str(exc), status=401)
        except DownloaderError as exc:
            return _save_result(False, "Gerade nicht möglich", str(exc),
                                status=502)
        db.add_post(result["shortcode"], result["username"],
                    result["type"], result["posted_at"])
        db.mark_downloaded(result["shortcode"], result["username"])
        sub = "Videos" if result["type"] in ("Video", "Reel", "Video/Reel") \
            else "Fotos"
        return _save_result(
            True, "Gespeichert",
            f"@{result['username']} · {result['type']} → Ordner: {sub}. "
            "Du kannst dieses Fenster schließen.")

    # ------------------------------------------------------------------
    # PWA: als App aufs Handy/den Desktop installierbar
    # ------------------------------------------------------------------
    _ICON_SVG = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
        '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#feda75"/><stop offset=".5" stop-color="#d62976"/>'
        '<stop offset="1" stop-color="#4f5bd5"/></linearGradient></defs>'
        '<rect width="512" height="512" rx="112" fill="url(#g)"/>'
        '<path d="M256 168v120m0 0l-52-52m52 52l52-52" fill="none" stroke="#fff" '
        'stroke-width="26" stroke-linecap="round" stroke-linejoin="round"/>'
        '<rect x="160" y="320" width="192" height="26" rx="13" fill="#fff"/></svg>'
    )

    @app.get("/background")
    def background():
        """Liefert täglich ein anderes bereits heruntergeladenes Foto als
        Hintergrundbild (stabil pro Tag). 404, wenn noch nichts geladen ist."""
        root = settings.download_dir
        exts = {".jpg", ".jpeg", ".png", ".webp"}
        try:
            files = sorted(
                str(p) for p in root.rglob("*")
                if p.is_file() and p.suffix.lower() in exts
            )
        except Exception:
            files = []
        if not files:
            return Response(status=404)
        day = datetime.date.today().isoformat()
        idx = int(hashlib.md5(day.encode()).hexdigest(), 16) % len(files)
        try:
            resp = send_file(files[idx])
        except Exception:
            return Response(status=404)
        # Die URL enthält das Datum (?d=…), daher darf der Browser das Bild
        # für den Rest des Tages aus dem Cache nehmen → schnellere Starts.
        resp.headers["Cache-Control"] = "private, max-age=3600"
        return resp

    @app.get("/icon.svg")
    def icon_svg():
        return Response(_ICON_SVG, mimetype="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/manifest.webmanifest")
    def manifest():
        return jsonify({
            "name": "Insta-X Monitor",
            "short_name": "Insta-X",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#000000",
            "theme_color": "#000000",
            "icons": [
                {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml",
                 "purpose": "any maskable"},
            ],
            # Teilen-Ziel: In der echten Instagram-App „Teilen → Instagram
            # Monitor" schickt den Beitragslink an GET /save, das ihn direkt
            # herunterlädt. (Nur verfügbar, wenn die Seite als App installiert
            # ist – dafür ist ein sicherer Kontext nötig: der eingebaute
            # Android-App-Server auf 127.0.0.1 bzw. HTTPS.)
            "share_target": {
                "action": "/save",
                "method": "GET",
                "params": {"title": "title", "text": "text", "url": "url"},
            },
        })

    @app.get("/sw.js")
    def service_worker():
        # Minimaler Service-Worker: ermöglicht die Installation als App.
        # (Kein aggressives Caching, damit immer die aktuelle Version läuft.)
        js = (
            "self.addEventListener('install', e => self.skipWaiting());\n"
            "self.addEventListener('activate', e => self.clients.claim());\n"
            "self.addEventListener('fetch', e => {});\n"
        )
        return Response(js, mimetype="application/javascript",
                        headers={"Cache-Control": "no-cache"})

    # ------------------------------------------------------------------
    # Automatischer Zeitplan (täglich zu festen Uhrzeiten herunterladen)
    # ------------------------------------------------------------------
    @app.get("/api/health")
    def health():
        return jsonify(ok=True, logged_in=downloader.is_logged_in,
                       user=downloader.logged_in_user or "")

    @app.get("/api/downloads")
    def downloads_list():
        rows = db.recent_downloads(200)
        return jsonify(count=len(rows), items=[{
            "shortcode": r["shortcode"],
            "username": r["username"],
            "type": r["post_type"],
            "downloaded_at": r["downloaded_at"],
            "url": f"https://www.instagram.com/p/{r['shortcode']}/",
        } for r in rows])

    # Kurzlebiger Cache für die Download-Statistik (reines Datei-Scannen,
    # kein Instagram-Zugriff – aber bei vielen Dateien trotzdem Arbeit).
    _stats_cache: dict = {"t": 0.0, "data": None}

    @app.get("/api/stats")
    def stats():
        """Download-Statistik: Anzahl Dateien und Speichergröße je Konto.

        Jedes Konto hat einen eigenen Unterordner im Download-Verzeichnis
        (so lädt der Downloader). Sortiert nach Größe, größtes zuerst.
        """
        now = time.time()
        if _stats_cache["data"] is not None and now - _stats_cache["t"] < 60:
            return jsonify(_stats_cache["data"])
        root = settings.download_dir
        accounts = []
        total_files = 0
        total_bytes = 0
        try:
            with os.scandir(root) as it:
                for entry in it:
                    try:
                        # Einzelne unlesbare Einträge überspringen (z. B.
                        # SD-Karte/Termux) statt die ganze Liste zu verwerfen.
                        if not entry.is_dir():
                            continue
                    except OSError:
                        continue
                    files = 0
                    size = 0
                    for dirpath, _dirs, names in os.walk(entry.path):
                        for n in names:
                            try:
                                size += os.path.getsize(
                                    os.path.join(dirpath, n))
                                files += 1
                            except OSError:
                                continue
                    if files:
                        accounts.append({"username": entry.name,
                                         "files": files, "bytes": size})
                        total_files += files
                        total_bytes += size
        except OSError:
            pass
        accounts.sort(key=lambda a: a["bytes"], reverse=True)
        # Freier Speicherplatz auf dem Datenträger des Download-Ordners.
        try:
            usage = shutil.disk_usage(root)
            disk = {"free": usage.free, "total": usage.total}
        except OSError:
            disk = {"free": 0, "total": 0}
        data = {"accounts": accounts, "total_files": total_files,
                "total_bytes": total_bytes, "dir": str(root), "disk": disk}
        _stats_cache["t"] = now
        _stats_cache["data"] = data
        return jsonify(data)

    @app.post("/api/backup")
    def backup_db():
        """Erstellt eine datierte Sicherheitskopie der Datenbank."""
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        target = db.path.parent / "backups" / f"monitor-{stamp}.db"
        try:
            db.backup(target)
        except Exception as exc:  # sqlite3-/OS-Fehler
            logger.exception("Datenbank-Backup fehlgeschlagen.")
            return jsonify(error=f"Backup fehlgeschlagen: {exc}"), 500
        return jsonify(ok=True, path=str(target))

    @app.get("/api/failed-downloads")
    def failed_downloads():
        """Beiträge, die zum Download vorgemerkt, aber noch nicht geladen sind."""
        rows = db.all_pending_downloads(500)
        return jsonify(count=len(rows), items=[{
            "shortcode": r["shortcode"],
            "username": r["username"],
            "type": r["post_type"],
            "posted_at": r["posted_at"],
            "url": f"https://www.instagram.com/p/{r['shortcode']}/",
        } for r in rows])

    _retry = _BulkJob()

    @app.post("/api/retry-failed")
    def retry_failed():
        """Versucht alle offenen Downloads erneut (im Hintergrund)."""
        if _retry.snapshot()["running"]:
            return jsonify(error="Eine Wiederholung läuft bereits."), 409
        rows = db.all_pending_downloads(500)
        if not rows:
            return jsonify(ok=True, total=0)
        pending = [(r["shortcode"], r["username"], r["post_type"],
                    r["posted_at"]) for r in rows]
        if not _retry.try_start(total=len(pending)):
            return jsonify(error="Eine Wiederholung läuft bereits."), 409

        def worker():
            done = 0
            try:
                for index, (sc, user, ptype, posted) in enumerate(pending):
                    if _retry.cancel:
                        break
                    _retry.update(current=user)
                    info = PostInfo(shortcode=sc, username=user,
                                    post_type=ptype or "", posted_at=posted or "",
                                    url=f"https://www.instagram.com/p/{sc}/")
                    try:
                        if downloader.download_post(info):
                            db.mark_downloaded(sc, user)
                            done += 1
                            _retry.update(downloaded=done)
                    except DownloaderError as exc:
                        logger.warning("Wiederholung %s fehlgeschlagen: %s", sc, exc)
                    _retry.update(done=index + 1)
                    if index < len(pending) - 1 and not _retry.cancel:
                        time.sleep(_BULK_DELAY + random.uniform(0.0, 2.0))
            except Exception:
                logger.exception("Download-Wiederholung abgebrochen.")
                _retry.update(error="Unerwarteter Fehler – siehe logs/web.log.")
            finally:
                _retry.update(running=False, finished=True)

        thread = threading.Thread(target=worker, name="retry-failed", daemon=True)
        _retry.thread = thread
        thread.start()
        return jsonify(ok=True, total=len(pending))

    @app.get("/api/retry-failed/status")
    def retry_failed_status():
        return jsonify(_retry.snapshot())

    @app.post("/api/last-posts")
    def last_posts():
        """Letzter bekannter Beitrags-Zeitpunkt je Konto.

        Kommt NUR aus der lokalen Datenbank (was Feed, Profil-Ansichten,
        Downloads und Prüfungen bereits gesehen haben) – kein
        Instagram-Abruf, daher sofort da.
        """
        data = request.get_json(silent=True) or {}
        usernames = [str(u).strip().lstrip("@")
                     for u in (data.get("usernames") or []) if str(u).strip()]
        items = {u: (db.newest_posted_at(u) or "") for u in usernames[:2000]}
        return jsonify(items=items)

    # ------------------------------------------------------------------
    # „Neue Beiträge prüfen“: geht die Konten der Liste einzeln durch
    # (rücksichtsvoll mit Pause), merkt sich Gefundenes in der Datenbank
    # und meldet, wer seit dem letzten Kenntnisstand etwas gepostet hat.
    # Läuft NUR auf Knopfdruck.
    # ------------------------------------------------------------------
    _check_lock = threading.Lock()
    _check_state: dict = {"running": False, "total": 0, "done": 0,
                          "current": "", "finished": False, "results": {}}
    _check_cancel = {"flag": False}

    def _run_check(usernames: list[str]) -> None:
        for i, u in enumerate(usernames):
            if _check_cancel["flag"]:
                break
            with _check_lock:
                _check_state["current"] = u
            info: dict = {"last": db.newest_posted_at(u) or "", "new": False}
            try:
                posts = downloader.list_recent_posts(u, 3)
                ref = info["last"]
                newest = ref
                for p in posts:
                    if not db.is_post_known(p.shortcode, u):
                        db.add_post(p.shortcode, u, p.post_type, p.posted_at)
                        # Nur als NEU werten, was jünger ist als der bisher
                        # jüngste bekannte Beitrag (erste Prüfung = Basis).
                        if ref and p.posted_at and p.posted_at > ref:
                            info["new"] = True
                    if p.posted_at and (not newest or p.posted_at > newest):
                        newest = p.posted_at
                info["last"] = newest or ""
            except DownloaderError as exc:
                info["error"] = str(exc)
            except Exception:
                logger.debug("Prüfung von %s fehlgeschlagen.", u, exc_info=True)
                info["error"] = "Fehler bei der Prüfung"
            db.set_activity_checked(u)
            with _check_lock:
                _check_state["results"][u] = info
                _check_state["done"] = i + 1
            time.sleep(_FEED_DELAY + random.uniform(0.0, 1.0))
        with _check_lock:
            _check_state["running"] = False
            _check_state["finished"] = True
            _check_state["current"] = ""
        # Prüfergebnisse liegen jetzt in der Datenbank → Oberfläche soll
        # sie beim nächsten Übersichts-Abruf sehen.
        db.bump_social_version()

    @app.post("/api/check-new")
    def check_new_start():
        if not downloader.is_logged_in:
            return jsonify(error="Bitte zuerst anmelden."), 401
        data = request.get_json(silent=True) or {}
        usernames = [str(u).strip().lstrip("@")
                     for u in (data.get("usernames") or []) if str(u).strip()]
        usernames = usernames[:500]
        if not usernames:
            return jsonify(error="Keine Konten übergeben."), 400
        with _check_lock:
            if _check_state["running"]:
                return jsonify(error="Es läuft bereits eine Prüfung."), 409
            _check_state.update(running=True, total=len(usernames), done=0,
                                current="", finished=False, results={})
            _check_cancel["flag"] = False
        threading.Thread(target=_run_check, args=(usernames,),
                         daemon=True, name="check-new").start()
        return jsonify(ok=True, total=len(usernames))

    @app.get("/api/check-new/status")
    def check_new_status():
        with _check_lock:
            return jsonify(**_check_state)

    @app.post("/api/check-new/cancel")
    def check_new_cancel():
        _check_cancel["flag"] = True
        return jsonify(ok=True)

    @app.get("/api/settings")
    def get_settings():
        return jsonify(save_captions=settings.save_captions,
                       download_dir=str(settings.download_dir),
                       social_auto_refresh=settings.social_auto_refresh,
                       social_refresh_minutes=settings.social_refresh_minutes,
                       quiet_enabled=settings.quiet_enabled,
                       quiet_from=settings.quiet_from,
                       quiet_until=settings.quiet_until)

    @app.post("/api/settings")
    def set_settings():
        data = request.get_json(silent=True) or {}
        if "save_captions" in data:
            settings.save_captions = bool(data["save_captions"])
        if "social_auto_refresh" in data:
            settings.social_auto_refresh = bool(data["social_auto_refresh"])
        if "social_refresh_minutes" in data:
            try:
                settings.social_refresh_minutes = int(data["social_refresh_minutes"])
            except (TypeError, ValueError):
                pass
        if "quiet_enabled" in data:
            settings.quiet_enabled = bool(data["quiet_enabled"])
        if "quiet_from" in data:
            settings.quiet_from = str(data["quiet_from"] or "")
        if "quiet_until" in data:
            settings.quiet_until = str(data["quiet_until"] or "")
        if "download_dir" in data:
            # Download-Ordner aus der Oberfläche ändern – z. B. auf einen
            # TeraBox-/Google-Drive-Sync-Ordner, dann landet alles
            # automatisch in der Cloud. Vorher prüfen, ob beschreibbar.
            raw = str(data["download_dir"] or "").strip()
            if raw:
                try:
                    target = Path(raw).expanduser().resolve()
                    target.mkdir(parents=True, exist_ok=True)
                    probe = target / ".schreibtest"
                    probe.write_text("ok", encoding="utf-8")
                    probe.unlink()
                except OSError as exc:
                    return jsonify(error=f"Ordner nicht nutzbar: {exc}"), 400
                settings.download_dir = target
        return jsonify(ok=True, save_captions=settings.save_captions,
                       download_dir=str(settings.download_dir),
                       social_auto_refresh=settings.social_auto_refresh,
                       social_refresh_minutes=settings.social_refresh_minutes)

    @app.get("/api/schedule")
    def get_schedule():
        return jsonify(
            enabled=settings.schedule_enabled,
            times=settings.schedule_times,
            per=settings.schedule_per,
            stories=settings.schedule_stories,
            last_run=settings.schedule_last_run,
        )

    @app.post("/api/schedule")
    def set_schedule():
        data = request.get_json(silent=True) or {}
        settings.schedule_enabled = bool(data.get("enabled"))
        settings.schedule_stories = bool(data.get("stories"))
        if "times" in data and isinstance(data["times"], list):
            settings.schedule_times = data["times"]
        if "per" in data:
            try:
                settings.schedule_per = max(1, min(_MAX_COUNT, int(data["per"])))
            except (TypeError, ValueError):
                pass
        return jsonify(
            ok=True,
            enabled=settings.schedule_enabled,
            times=settings.schedule_times,
            per=settings.schedule_per,
            stories=settings.schedule_stories,
        )

    def _scheduler_loop():
        """Prüft jede Minute, ob ein geplanter Download fällig ist."""
        fired: set[str] = set()   # Schlüssel "YYYY-MM-DD HH:MM" (kein Doppelstart)
        while True:
            try:
                if settings.schedule_enabled:
                    now = datetime.datetime.now()
                    hhmm = now.strftime("%H:%M")
                    if hhmm in settings.schedule_times:
                        key = now.strftime("%Y-%m-%d ") + hhmm
                        if key not in fired:
                            fired.add(key)
                            if len(fired) > 64:  # alte Marker gelegentlich aufräumen
                                fired = set(sorted(fired)[-16:])
                            _run_scheduled(now)
            except Exception:
                logger.exception("Zeitplan-Prüfung fehlgeschlagen.")
            time.sleep(20)

    def _run_scheduled(now: "datetime.datetime") -> None:
        if not downloader.is_logged_in:
            logger.warning(
                "Zeitplan %s: nicht angemeldet – Download übersprungen. "
                "Bitte in der Oberfläche anmelden.", now.strftime("%H:%M")
            )
            return
        try:
            usernames = downloader.following_usernames()
        except DownloaderError as exc:
            logger.warning("Zeitplan %s: Abo-Liste nicht abrufbar: %s",
                           now.strftime("%H:%M"), exc)
            return
        if not usernames:
            logger.info("Zeitplan %s: keine Abos.", now.strftime("%H:%M"))
            return
        if start_bulk_job(usernames, settings.schedule_per, "",
                          also_stories=settings.schedule_stories):
            settings.schedule_last_run = now.strftime("%Y-%m-%d %H:%M")
            logger.info("Zeitplan %s: automatischer Download von %d Konten "
                        "gestartet%s.", now.strftime("%H:%M"), len(usernames),
                        " (inkl. Stories)" if settings.schedule_stories else "")
        else:
            logger.info("Zeitplan %s: übersprungen (ein Download läuft bereits).",
                        now.strftime("%H:%M"))

    threading.Thread(target=_scheduler_loop, name="scheduler", daemon=True).start()

    return app
