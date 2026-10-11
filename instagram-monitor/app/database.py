"""
SQLite-Persistenzschicht der Anwendung.
=======================================

Verwaltet fünf Tabellen:

  accounts   – die überwachten Instagram-Konten inkl. Download-Option,
               Zeitpunkt der letzten Prüfung und Baseline-Status
  posts      – alle bekannten Beiträge pro Konto; verhindert doppelte
               Benachrichtigungen und doppelte Downloads und merkt sich
               fehlgeschlagene Downloads für spätere Wiederholungen.
               Primärschlüssel ist (shortcode, username): Collab-Beiträge
               erscheinen unter demselben Shortcode auf MEHREREN Profilen
               und werden pro überwachtem Konto einzeln geführt.
  settings   – Schlüssel/Wert-Paare für Anwendungseinstellungen
  social_accounts  – Zwischenspeicher der Gefolgt-/Follower-Listen des
               angemeldeten Kontos. Die Browser-Oberfläche liest beim
               Öffnen NUR diese Tabelle (sofort da, kein Instagram-Abruf);
               aufgefrischt wird im Hintergrund.
  account_activity – je Konto: bis wohin der Nutzer Beiträge „gesehen"
               hat (für die NEU-Markierung) und wann zuletzt geprüft wurde.

Thread-Sicherheit
-----------------
Die GUI (Haupt-Thread) und der Monitor (Hintergrund-Thread) greifen beide
auf die Datenbank zu. SQLite-Verbindungen sind standardmäßig an einen
Thread gebunden; deshalb wird die Verbindung mit
``check_same_thread=False`` geöffnet und JEDER Zugriff durch ein
``threading.Lock`` serialisiert. Bei der geringen Zugriffsfrequenz dieser
Anwendung ist das die einfachste und robusteste Lösung.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

# DDL der posts-Tabelle – separat definiert, weil die Migration älterer
# Datenbanken (siehe _migrate) die Tabelle neu aufbauen können muss.
_POSTS_DDL = """
CREATE TABLE IF NOT EXISTS posts (
    shortcode        TEXT NOT NULL,               -- eindeutige Instagram-Kurz-ID des Beitrags
    username         TEXT NOT NULL,               -- zu welchem Konto der Beitrag gehört
    post_type        TEXT NOT NULL DEFAULT '',    -- z. B. "Reel", "Video/Reel", "Album", "Bild"
    posted_at        TEXT,                        -- Veröffentlichungszeitpunkt (ISO, UTC)
    discovered_at    TEXT NOT NULL,               -- wann die Anwendung den Beitrag entdeckt hat
    downloaded       INTEGER NOT NULL DEFAULT 0,  -- 1 = wurde erfolgreich heruntergeladen
    downloaded_at    TEXT,                        -- Zeitpunkt des Downloads
    download_pending INTEGER NOT NULL DEFAULT 0,  -- 1 = Download gewünscht, aber noch nicht gelungen
    PRIMARY KEY (shortcode, username)
)
"""

# Schema der Datenbank. Wird bei jedem Start ausgeführt (idempotent).
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS accounts (
    username         TEXT PRIMARY KEY,            -- Instagram-Benutzername (klein geschrieben)
    download_enabled INTEGER NOT NULL DEFAULT 0,  -- 1 = neue Beiträge automatisch herunterladen
    baseline_done    INTEGER NOT NULL DEFAULT 0,  -- 1 = Erstinventur der vorhandenen Beiträge abgeschlossen
    last_checked     TEXT,                        -- ISO-Zeitstempel der letzten erfolgreichen Prüfung
    last_error       TEXT,                        -- letzte Fehlermeldung (für die Anzeige in der GUI)
    added_at         TEXT NOT NULL                -- ISO-Zeitstempel, wann das Konto hinzugefügt wurde
);

{_POSTS_DDL};

CREATE INDEX IF NOT EXISTS idx_posts_username ON posts (username);
CREATE INDEX IF NOT EXISTS idx_posts_user_posted ON posts (username, posted_at);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS social_accounts (
    kind      TEXT NOT NULL,              -- 'following' oder 'followers'
    username  TEXT NOT NULL,
    full_name TEXT NOT NULL DEFAULT '',
    pic_url   TEXT NOT NULL DEFAULT '',   -- letzte bekannte CDN-URL des Profilbilds
    position  INTEGER NOT NULL DEFAULT 0, -- Reihenfolge wie von Instagram geliefert
    PRIMARY KEY (kind, username)
);

CREATE TABLE IF NOT EXISTS account_activity (
    username     TEXT PRIMARY KEY,
    seen_post_at TEXT,   -- jüngster Beitrags-Zeitpunkt, den der Nutzer gesehen hat
    checked_at   TEXT,   -- wann „zuletzt gepostet" zuletzt geprüft wurde
    favorite     INTEGER NOT NULL DEFAULT 0,  -- ⭐ angepinnt (immer oben)
    group_name   TEXT NOT NULL DEFAULT '',    -- freie Gruppe/Etikett
    has_story    INTEGER NOT NULL DEFAULT 0   -- hat gerade eine aktive Story
);

CREATE TABLE IF NOT EXISTS social_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind      TEXT NOT NULL,      -- 'following' oder 'followers'
    username  TEXT NOT NULL,
    event     TEXT NOT NULL,      -- 'added' (neu) oder 'removed' (weg)
    happened_at TEXT NOT NULL     -- Zeitpunkt der Feststellung
);

CREATE TABLE IF NOT EXISTS x_posts (
    post_id       TEXT PRIMARY KEY,
    username      TEXT NOT NULL DEFAULT '',
    author_name   TEXT NOT NULL DEFAULT '',
    post_url      TEXT NOT NULL,
    search_query  TEXT NOT NULL DEFAULT '',
    caption       TEXT NOT NULL DEFAULT '',
    preview_url   TEXT NOT NULL DEFAULT '',
    published_at  TEXT,
    discovered_at TEXT NOT NULL,
    downloaded    INTEGER NOT NULL DEFAULT 0,
    downloaded_at TEXT,
    local_path    TEXT,
    last_error    TEXT
);

CREATE INDEX IF NOT EXISTS idx_x_posts_discovered ON x_posts (discovered_at DESC);
CREATE INDEX IF NOT EXISTS idx_x_posts_downloaded ON x_posts (downloaded, discovered_at DESC);
"""


def _now() -> str:
    """Aktueller Zeitstempel als ISO-String (sekundengenau, lokale Zeit)."""
    return datetime.now().isoformat(timespec="seconds")


def _now_utc() -> str:
    """Aktueller Zeitstempel in UTC mit Zeitzonen-Angabe (…+00:00).

    Wird für Zeitpunkte verwendet, die die Web-Oberfläche über ``timeAgo``
    anzeigt (z. B. „Stand: …“). Die Oberfläche interpretiert zeitzonenlose
    Werte als UTC; ein lokaler Zeitstempel würde dort um den UTC-Versatz
    (in Deutschland 1–2 Stunden) falsch erscheinen.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """Kapselt sämtliche Datenbankzugriffe der Anwendung."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        # Ein Lock serialisiert alle Zugriffe (GUI-Thread + Monitor-Thread).
        self._lock = threading.Lock()

        # check_same_thread=False erlaubt die Nutzung aus mehreren Threads;
        # die Serialisierung übernimmt unser Lock.
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            # WAL-Modus: Lesen und Schreiben behindern sich nicht mehr
            # gegenseitig. Web-Oberfläche und Monitor greifen gleichzeitig
            # zu – das verhindert die berüchtigten „database is locked"-Fehler.
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA busy_timeout=5000")
            except sqlite3.OperationalError:
                logger.warning("WAL-Modus nicht verfügbar – Standardmodus.")
            self._migrate()
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        logger.debug("Datenbank geöffnet: %s", self._db_path)

    @property
    def path(self) -> Path:
        """Pfad der Datenbank-Datei (z. B. für Cache-Ordner daneben, Backups)."""
        return self._db_path

    def _migrate(self) -> None:
        """Bringt ältere Datenbanken auf das aktuelle Schema (idempotent).

        ``CREATE TABLE IF NOT EXISTS`` ergänzt weder Spalten noch ändert es
        Primärschlüssel – deshalb hier explizit:
          1. Spalte ``download_pending`` nachrüsten.
          2. Alten Primärschlüssel (nur shortcode) durch den
             zusammengesetzten Schlüssel (shortcode, username) ersetzen
             (erfordert in SQLite einen Tabellen-Neuaufbau).
        Läuft bewusst VOR dem Schema-Skript: Bei einer frischen Datenbank
        existiert die Tabelle noch nicht und beide Schritte sind No-Ops.
        """
        # Schritt 1: fehlende Spalte ergänzen.
        try:
            self._conn.execute(
                "ALTER TABLE posts ADD COLUMN "
                "download_pending INTEGER NOT NULL DEFAULT 0"
            )
            logger.info("Datenbank-Migration: Spalte download_pending ergänzt.")
        except sqlite3.OperationalError:
            # Tabelle existiert nicht (frische DB) oder Spalte schon da.
            pass

        # Neue Spalten der account_activity-Tabelle nachrüsten (ältere DBs).
        for ddl in (
            "ALTER TABLE account_activity ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE account_activity ADD COLUMN group_name TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE account_activity ADD COLUMN has_story INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                self._conn.execute(ddl)
            except sqlite3.OperationalError:
                pass  # Tabelle fehlt noch (frische DB) oder Spalte existiert

        # Schritt 2: Primärschlüssel prüfen und ggf. Tabelle neu aufbauen.
        info = self._conn.execute("PRAGMA table_info(posts)").fetchall()
        pk_columns = [row["name"] for row in info if row["pk"]]
        if pk_columns == ["shortcode"]:
            logger.info(
                "Datenbank-Migration: posts-Tabelle wird auf den "
                "zusammengesetzten Schlüssel (shortcode, username) umgestellt."
            )
            self._conn.executescript(
                f"""
                ALTER TABLE posts RENAME TO posts_old;
                {_POSTS_DDL};
                INSERT OR IGNORE INTO posts
                    (shortcode, username, post_type, posted_at,
                     discovered_at, downloaded, downloaded_at, download_pending)
                SELECT shortcode, username, post_type, posted_at,
                       discovered_at, downloaded, downloaded_at, download_pending
                FROM posts_old;
                DROP TABLE posts_old;
                """
            )

    # ------------------------------------------------------------------
    # interne Helfer
    # ------------------------------------------------------------------
    def _execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        """Führt ein Statement unter dem Lock aus und committet sofort."""
        with self._lock:
            cursor = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cursor

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        """Führt eine Leseabfrage unter dem Lock aus."""
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    # ------------------------------------------------------------------
    # Konten
    # ------------------------------------------------------------------
    def add_account(self, username: str) -> bool:
        """Fügt ein Konto hinzu.

        Rückgabe: True, wenn das Konto neu angelegt wurde,
                  False, wenn es bereits existierte.
        """
        username = username.strip().lstrip("@").lower()
        if not username:
            return False
        try:
            self._execute(
                "INSERT INTO accounts (username, added_at) VALUES (?, ?)",
                (username, _now()),
            )
            logger.info("Konto hinzugefügt: @%s", username)
            return True
        except sqlite3.IntegrityError:
            # Primary-Key-Verletzung → Konto existiert bereits.
            logger.info("Konto @%s ist bereits vorhanden.", username)
            return False

    def remove_account(self, username: str) -> None:
        """Entfernt ein Konto aus der Überwachung.

        Die bereits bekannten Beiträge (Tabelle ``posts``) bleiben bewusst
        erhalten: Wird das Konto später erneut hinzugefügt, verhindern sie,
        dass alte Beiträge noch einmal heruntergeladen werden.
        """
        self._execute("DELETE FROM accounts WHERE username = ?", (username,))
        logger.info("Konto entfernt: @%s", username)

    def get_accounts(self) -> list[sqlite3.Row]:
        """Alle überwachten Konten, alphabetisch sortiert."""
        return self._query("SELECT * FROM accounts ORDER BY username")

    def set_download_enabled(self, username: str, enabled: bool) -> None:
        """Schaltet den automatischen Download für ein Konto ein/aus."""
        self._execute(
            "UPDATE accounts SET download_enabled = ? WHERE username = ?",
            (1 if enabled else 0, username),
        )

    def mark_baseline_done(self, username: str) -> None:
        """Markiert die Erstinventur eines Kontos als abgeschlossen.

        Beim ersten Prüflauf werden alle bereits existierenden Beiträge nur
        registriert (Baseline), damit nicht der gesamte Verlauf des Kontos
        als "neu" gemeldet bzw. heruntergeladen wird.
        """
        self._execute(
            "UPDATE accounts SET baseline_done = 1 WHERE username = ?",
            (username,),
        )

    def update_last_checked(self, username: str, error: Optional[str] = None) -> None:
        """Speichert Zeitpunkt und Ergebnis (Fehlertext oder None) der Prüfung."""
        self._execute(
            "UPDATE accounts SET last_checked = ?, last_error = ? WHERE username = ?",
            (_now(), error, username),
        )

    # ------------------------------------------------------------------
    # Beiträge
    # ------------------------------------------------------------------
    def is_post_known(self, shortcode: str, username: str) -> bool:
        """Wurde dieser Beitrag für dieses Konto schon registriert?

        Bewusst pro Konto (nicht global): Collab-Beiträge tragen auf allen
        beteiligten Profilen denselben Shortcode und sollen für jedes
        überwachte Konto einzeln gemeldet/heruntergeladen werden.
        """
        rows = self._query(
            "SELECT 1 FROM posts WHERE shortcode = ? AND username = ?",
            (shortcode, username),
        )
        return bool(rows)

    def add_post(
        self,
        shortcode: str,
        username: str,
        post_type: str = "",
        posted_at: Optional[str] = None,
        download_pending: bool = False,
    ) -> None:
        """Registriert einen Beitrag als bekannt (noch nicht heruntergeladen).

        ``download_pending=True`` merkt den Beitrag für den automatischen
        Download vor; das Flag wird erst durch :meth:`mark_downloaded`
        gelöscht, sodass fehlgeschlagene Downloads bei späteren Prüfläufen
        automatisch erneut versucht werden.
        """
        self._execute(
            """
            INSERT OR IGNORE INTO posts
                (shortcode, username, post_type, posted_at, discovered_at,
                 download_pending)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                shortcode,
                username,
                post_type,
                posted_at,
                _now(),
                1 if download_pending else 0,
            ),
        )

    def mark_downloaded(self, shortcode: str, username: str) -> None:
        """Markiert einen Beitrag als erfolgreich heruntergeladen."""
        self._execute(
            """
            UPDATE posts
            SET downloaded = 1, downloaded_at = ?, download_pending = 0
            WHERE shortcode = ? AND username = ?
            """,
            (_now(), shortcode, username),
        )

    def get_pending_downloads(self, username: str) -> list[sqlite3.Row]:
        """Beiträge, deren gewünschter Download noch aussteht (älteste zuerst)."""
        return self._query(
            """
            SELECT shortcode, post_type, posted_at FROM posts
            WHERE username = ? AND download_pending = 1 AND downloaded = 0
            ORDER BY posted_at
            """,
            (username,),
        )

    def all_pending_downloads(self, limit: int = 500) -> list[sqlite3.Row]:
        """Alle noch offenen (vorgemerkten) Downloads über ALLE Konten.

        Für die „fehlgeschlagene Downloads"-Ansicht in der Web-Oberfläche:
        Beiträge, die zum Download vorgemerkt sind, aber (z. B. wegen
        Drosselung) noch nicht geladen wurden.
        """
        return self._query(
            """
            SELECT shortcode, username, post_type, posted_at, discovered_at
            FROM posts
            WHERE download_pending = 1 AND downloaded = 0
            ORDER BY discovered_at DESC LIMIT ?
            """,
            (int(limit),),
        )

    def mark_pending(self, shortcode: str, username: str) -> None:
        """Merkt einen bekannten Beitrag (erneut) zum Download vor."""
        self._execute(
            "UPDATE posts SET download_pending = 1 "
            "WHERE shortcode = ? AND username = ?",
            (shortcode, username),
        )

    def newest_posted_at(self, username: str) -> Optional[str]:
        """Veröffentlichungszeitpunkt des jüngsten bekannten Beitrags.

        Dient als Referenz, um alte Beiträge zu erkennen, die (z. B. durch
        An-/Abpinnen) wieder in das Sichtfenster der neuesten N Beiträge
        rücken: Sie sind älter als dieser Zeitpunkt und damit keine
        Neuveröffentlichungen. None, wenn noch kein Beitrag bekannt ist.
        """
        rows = self._query(
            "SELECT MAX(posted_at) AS newest FROM posts WHERE username = ?",
            (username,),
        )
        return rows[0]["newest"] if rows else None

    def find_post(self, shortcode: str) -> Optional[sqlite3.Row]:
        """Ein bekannter Beitrag anhand seines Shortcodes (Konto beliebig).

        Dient dazu, nach einem Server-Neustart aus einem persistierten
        Shortcode (Sofort-Feed/Verlauf) wieder ein vollständiges Beitrags-
        Objekt zu rekonstruieren – ohne den flüchtigen In-Memory-Cache.
        Liefert ``username``, ``post_type`` und ``posted_at`` oder None.
        """
        rows = self._query(
            "SELECT username, post_type, posted_at FROM posts "
            "WHERE shortcode = ? ORDER BY downloaded DESC LIMIT 1",
            (shortcode,),
        )
        return rows[0] if rows else None

    def is_downloaded(self, shortcode: str, username: str) -> bool:
        """Wurde dieser Beitrag für dieses Konto bereits heruntergeladen?"""
        rows = self._query(
            "SELECT 1 FROM posts WHERE shortcode = ? AND username = ? "
            "AND downloaded = 1",
            (shortcode, username),
        )
        return bool(rows)

    def recent_downloads(self, limit: int = 200) -> list[sqlite3.Row]:
        """Zuletzt heruntergeladene Beiträge (neueste zuerst) – für den Verlauf."""
        return self._query(
            "SELECT shortcode, username, post_type, downloaded_at FROM posts "
            "WHERE downloaded = 1 AND downloaded_at IS NOT NULL "
            "ORDER BY downloaded_at DESC LIMIT ?",
            (int(limit),),
        )

    def download_count(self, username: str) -> int:
        """Anzahl der für dieses Konto heruntergeladenen Beiträge."""
        rows = self._query(
            "SELECT COUNT(*) AS n FROM posts WHERE username = ? AND downloaded = 1",
            (username,),
        )
        return int(rows[0]["n"]) if rows else 0

    # ------------------------------------------------------------------
    # Einstellungen (Schlüssel/Wert)
    # ------------------------------------------------------------------
    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        rows = self._query("SELECT value FROM settings WHERE key = ?", (key,))
        return rows[0]["value"] if rows else default

    def set_setting(self, key: str, value: str) -> None:
        self._execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    # ------------------------------------------------------------------
    # Soziale Listen (Gefolgt/Follower) – Zwischenspeicher für die
    # Browser-Oberfläche: Öffnen der Seite liest NUR diese Daten.
    # ------------------------------------------------------------------
    def replace_social_list(self, kind: str, items: list[dict]) -> None:
        """Ersetzt die komplette Liste einer Art ('following'/'followers').

        ``items``: [{"username", "full_name", "pic"}, …] in der von
        Instagram gelieferten Reihenfolge. Läuft als EINE Transaktion,
        damit Leser nie eine halb ersetzte Liste sehen. Merkt sich den
        Zeitpunkt der Auffrischung und erhöht die Versionsnummer, damit
        die Oberfläche Änderungen erkennt.
        """
        # UTC, weil die Oberfläche diese Zeit über ``timeAgo`` anzeigt.
        now = _now_utc()
        with self._lock:
            # Verlauf: Wer ist neu dazugekommen, wer ist weg? (Nur wenn es
            # schon eine alte Liste gab – sonst wäre alles "neu".)
            old = {
                row["username"]
                for row in self._conn.execute(
                    "SELECT username FROM social_accounts WHERE kind = ?",
                    (kind,),
                )
            }
            new = {
                str(item.get("username", "")).lower()
                for item in items if item.get("username")
            }
            if old:
                events = (
                    [(kind, u, "added", now) for u in sorted(new - old)]
                    + [(kind, u, "removed", now) for u in sorted(old - new)]
                )
                if events:
                    self._conn.executemany(
                        "INSERT INTO social_events "
                        "(kind, username, event, happened_at) "
                        "VALUES (?, ?, ?, ?)",
                        events,
                    )
            self._conn.execute(
                "DELETE FROM social_accounts WHERE kind = ?", (kind,)
            )
            self._conn.executemany(
                """
                INSERT OR REPLACE INTO social_accounts
                    (kind, username, full_name, pic_url, position)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        kind,
                        str(item.get("username", "")).lower(),
                        item.get("full_name", "") or "",
                        item.get("pic", "") or "",
                        position,
                    )
                    for position, item in enumerate(items)
                    if item.get("username")
                ],
            )
            self._conn.execute(
                """
                INSERT INTO settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (f"social_{kind}_fetched_at", now),
            )
            self._conn.commit()
        self.bump_social_version()
        logger.info("Soziale Liste '%s' aktualisiert (%d Konten).",
                    kind, len(items))

    def get_social_list(self, kind: str) -> list[sqlite3.Row]:
        """Liste einer Art inkl. „zuletzt gepostet" und Gesehen-Stand.

        ``last_post_at`` ist der jüngste in ``posts`` bekannte Beitrag des
        Kontos (kommt also ausschließlich aus lokalen Daten – sofort da).
        """
        return self._query(
            """
            SELECT s.username, s.full_name, s.pic_url, s.position,
                   (SELECT MAX(p.posted_at) FROM posts p
                    WHERE p.username = s.username) AS last_post_at,
                   a.seen_post_at, a.checked_at,
                   COALESCE(a.favorite, 0)   AS favorite,
                   COALESCE(a.group_name,'') AS group_name,
                   COALESCE(a.has_story, 0)  AS has_story
            FROM social_accounts s
            LEFT JOIN account_activity a ON a.username = s.username
            WHERE s.kind = ?
            ORDER BY s.position
            """,
            (kind,),
        )

    def social_fetched_at(self, kind: str) -> str:
        """Wann die Liste zuletzt von Instagram geholt wurde ("" = noch nie)."""
        return self.get_setting(f"social_{kind}_fetched_at", "") or ""

    def social_version(self) -> int:
        """Laufende Versionsnummer der sozialen Daten (für die Oberfläche)."""
        try:
            return int(self.get_setting("social_version", "0") or "0")
        except ValueError:
            return 0

    def bump_social_version(self) -> None:
        """Erhöht die Versionsnummer (Oberfläche rendert nur bei Änderung neu)."""
        self._execute(
            """
            INSERT INTO settings (key, value) VALUES ('social_version', '1')
            ON CONFLICT(key) DO UPDATE
            SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)
            """
        )

    def overview_etag(self) -> str:
        """Kompakte Änderungsmarke der Übersichtsdaten.

        Kombiniert die soziale Versionsnummer mit der höchsten rowid der
        posts-Tabelle: Jeder neu bekannte Beitrag (Prüfung, Feed, Download)
        ändert damit die Marke, ohne dass teure Vergleiche nötig sind.
        """
        rows = self._query("SELECT COALESCE(MAX(rowid), 0) AS m FROM posts")
        max_rowid = int(rows[0]["m"]) if rows else 0
        return f"{self.social_version()}-{max_rowid}"

    def mark_account_seen(self, username: str) -> None:
        """Merkt: Der Nutzer hat den aktuellen Stand dieses Kontos gesehen.

        Setzt die Gesehen-Marke auf den jüngsten bekannten Beitrag; die
        NEU-Markierung erlischt damit, bis wieder etwas Neueres bekannt wird.
        """
        username = username.strip().lstrip("@").lower()
        if not username:
            return
        newest = self.newest_posted_at(username) or ""
        self._execute(
            """
            INSERT INTO account_activity (username, seen_post_at)
            VALUES (?, ?)
            ON CONFLICT(username) DO UPDATE SET seen_post_at = excluded.seen_post_at
            """,
            (username, newest),
        )
        self.bump_social_version()

    def set_activity_checked(self, username: str) -> None:
        """Merkt den Zeitpunkt der letzten „zuletzt gepostet"-Prüfung."""
        username = username.strip().lstrip("@").lower()
        if not username:
            return
        self._execute(
            """
            INSERT INTO account_activity (username, checked_at)
            VALUES (?, ?)
            ON CONFLICT(username) DO UPDATE SET checked_at = excluded.checked_at
            """,
            (username, _now()),
        )

    def set_favorite(self, username: str, on: bool) -> None:
        """⭐ Konto anpinnen/lösen (Favoriten stehen in den Listen oben)."""
        username = username.strip().lstrip("@").lower()
        if not username:
            return
        self._execute(
            """
            INSERT INTO account_activity (username, favorite)
            VALUES (?, ?)
            ON CONFLICT(username) DO UPDATE SET favorite = excluded.favorite
            """,
            (username, 1 if on else 0),
        )
        self.bump_social_version()

    def set_group(self, username: str, group: str) -> None:
        """Freie Gruppe/Etikett eines Kontos setzen ("" = keine Gruppe)."""
        username = username.strip().lstrip("@").lower()
        if not username:
            return
        group = (group or "").strip()[:40]
        self._execute(
            """
            INSERT INTO account_activity (username, group_name)
            VALUES (?, ?)
            ON CONFLICT(username) DO UPDATE SET group_name = excluded.group_name
            """,
            (username, group),
        )
        self.bump_social_version()

    def mark_account_complete(self, username: str) -> None:
        """Merkt: Von diesem Konto wurde ALLES geladen (Komplett-Lauf).

        Speichert Zeitpunkt und die Zahl der bekannten Beiträge, damit die
        „Erledigt"-Liste zeigen kann, wann und wie viel geladen wurde.
        """
        username = username.strip().lstrip("@").lower()
        if not username:
            return
        rows = self._query(
            "SELECT COUNT(*) AS n FROM posts WHERE username = ? AND downloaded = 1",
            (username,),
        )
        n = int(rows[0]["n"]) if rows else 0
        self._execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (f"complete_{username}", f"{_now()}|{n}"),
        )
        self.bump_social_version()

    def completed_accounts(self) -> list[dict]:
        """Alle als komplett geladen vermerkten Konten (neueste zuerst)."""
        rows = self._query(
            "SELECT key, value FROM settings WHERE key LIKE 'complete\\_%' ESCAPE '\\'"
        )
        out = []
        for r in rows:
            username = r["key"][len("complete_"):]
            when, _, count = (r["value"] or "").partition("|")
            try:
                n = int(count)
            except ValueError:
                n = 0
            out.append({"username": username, "at": when, "files": n})
        out.sort(key=lambda d: d["at"], reverse=True)
        return out

    def is_account_complete(self, username: str) -> bool:
        """Wurde von diesem Konto schon einmal ALLES geladen?"""
        username = username.strip().lstrip("@").lower()
        return bool(username and self.get_setting(f"complete_{username}", ""))

    def clear_account_complete(self, username: str) -> None:
        """Erledigt-Vermerk wieder entfernen (z. B. um neu zu laden)."""
        username = username.strip().lstrip("@").lower()
        if username:
            self._execute("DELETE FROM settings WHERE key = ?",
                          (f"complete_{username}",))
            self.bump_social_version()

    def is_favorite(self, username: str) -> bool:
        """Ist dieses Konto angepinnt (⭐)? Für Treffer, die (noch) nicht in
        einer Liste stehen (z. B. Suchergebnisse)."""
        username = username.strip().lstrip("@").lower()
        if not username:
            return False
        rows = self._query(
            "SELECT favorite FROM account_activity WHERE username = ?",
            (username,),
        )
        return bool(rows and rows[0]["favorite"])

    def add_social_account(self, kind: str, item: dict) -> None:
        """Fügt EIN Konto zur lokalen Liste ('following'/'followers') hinzu.

        Ergänzt die wholesale-Ersetzung (:meth:`replace_social_list`) um ein
        Einzel-Hinzufügen – z. B. „Folgen" aus der Suche. Neueinträge werden
        ans Ende gehängt (höchste ``position``) und als Ereignis 'added'
        vermerkt (nur wenn wirklich neu). Idempotent.
        """
        username = str(item.get("username", "")).strip().lstrip("@").lower()
        if not username:
            return
        now = _now_utc()
        with self._lock:
            existed = self._conn.execute(
                "SELECT position FROM social_accounts WHERE kind = ? AND username = ?",
                (kind, username),
            ).fetchone()
            row = self._conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 AS p "
                "FROM social_accounts WHERE kind = ?",
                (kind,),
            ).fetchone()
            position = existed["position"] if existed else int(row["p"])
            self._conn.execute(
                """
                INSERT OR REPLACE INTO social_accounts
                    (kind, username, full_name, pic_url, position)
                VALUES (?, ?, ?, ?, ?)
                """,
                (kind, username, item.get("full_name", "") or "",
                 item.get("pic", "") or "", position),
            )
            if not existed:
                self._conn.execute(
                    "INSERT INTO social_events (kind, username, event, happened_at) "
                    "VALUES (?, ?, 'added', ?)",
                    (kind, username, now),
                )
            self._conn.commit()
        self.bump_social_version()

    def remove_social_account(self, kind: str, username: str) -> bool:
        """Entfernt EIN Konto aus der lokalen Liste. Rückgabe: war es drin?"""
        username = username.strip().lstrip("@").lower()
        if not username:
            return False
        now = _now_utc()
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM social_accounts WHERE kind = ? AND username = ?",
                (kind, username),
            )
            removed = cur.rowcount > 0
            if removed:
                self._conn.execute(
                    "INSERT INTO social_events (kind, username, event, happened_at) "
                    "VALUES (?, ?, 'removed', ?)",
                    (kind, username, now),
                )
            self._conn.commit()
        if removed:
            self.bump_social_version()
        return removed

    def set_story_usernames(self, usernames: list[str]) -> None:
        """Merkt, welche Konten GERADE eine aktive Story haben.

        Setzt das Flag für alle übergebenen Konten und löscht es überall
        sonst – Ergebnis eines einzigen Instagram-Abrufs (Story-Leiste).
        """
        wanted = {u.strip().lstrip("@").lower() for u in usernames if u.strip()}
        with self._lock:
            self._conn.execute(
                "UPDATE account_activity SET has_story = 0 WHERE has_story = 1"
            )
            self._conn.executemany(
                """
                INSERT INTO account_activity (username, has_story)
                VALUES (?, 1)
                ON CONFLICT(username) DO UPDATE SET has_story = 1
                """,
                [(u,) for u in sorted(wanted)],
            )
            self._conn.commit()
        self.bump_social_version()

    def social_events(self, limit: int = 200) -> list[sqlite3.Row]:
        """Verlauf: Wer kam neu dazu / ist weg (neueste zuerst)."""
        return self._query(
            """
            SELECT kind, username, event, happened_at
            FROM social_events
            ORDER BY id DESC LIMIT ?
            """,
            (int(limit),),
        )

    # ------------------------------------------------------------------
    # X-Videobeiträge (getrennt von den Instagram-Tabellen)
    # ------------------------------------------------------------------
    def upsert_x_post(self, item: dict[str, Any]) -> None:
        """Fügt ein sichtbares X-Suchergebnis idempotent ein oder aktualisiert es."""
        self._execute(
            """
            INSERT INTO x_posts (
                post_id, username, author_name, post_url, search_query,
                caption, preview_url, published_at, discovered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(post_id) DO UPDATE SET
                username = excluded.username,
                author_name = excluded.author_name,
                post_url = excluded.post_url,
                search_query = excluded.search_query,
                caption = excluded.caption,
                preview_url = excluded.preview_url,
                published_at = excluded.published_at,
                discovered_at = excluded.discovered_at
            """,
            (
                str(item.get("post_id") or ""),
                str(item.get("username") or ""),
                str(item.get("author_name") or ""),
                str(item.get("post_url") or ""),
                str(item.get("search_query") or ""),
                str(item.get("caption") or ""),
                str(item.get("preview_url") or ""),
                str(item.get("published_at") or "") or None,
                _now_utc(),
            ),
        )

    def is_x_post_known(self, post_id: str) -> bool:
        rows = self._query("SELECT 1 FROM x_posts WHERE post_id = ? LIMIT 1", (post_id,))
        return bool(rows)

    def is_x_post_downloaded(self, post_id: str) -> bool:
        rows = self._query(
            "SELECT downloaded FROM x_posts WHERE post_id = ? LIMIT 1", (post_id,)
        )
        return bool(rows and rows[0]["downloaded"])

    def get_x_post(self, post_id: str) -> Optional[sqlite3.Row]:
        rows = self._query("SELECT * FROM x_posts WHERE post_id = ? LIMIT 1", (post_id,))
        return rows[0] if rows else None

    def get_x_posts(self, post_ids: list[str]) -> list[sqlite3.Row]:
        if not post_ids:
            return []
        placeholders = ",".join("?" for _ in post_ids)
        return self._query(
            f"SELECT * FROM x_posts WHERE post_id IN ({placeholders})",  # noqa: S608
            post_ids,
        )

    def recent_x_posts(self, limit: int = 50, only_new: bool = False) -> list[sqlite3.Row]:
        limit = max(1, min(50, int(limit)))
        where = "WHERE downloaded = 0" if only_new else ""
        return self._query(
            f"SELECT * FROM x_posts {where} ORDER BY discovered_at DESC LIMIT ?",  # noqa: S608
            (limit,),
        )

    def mark_x_post_downloaded(self, post_id: str, local_path: str) -> None:
        self._execute(
            """
            UPDATE x_posts
            SET downloaded = 1, downloaded_at = ?, local_path = ?, last_error = NULL
            WHERE post_id = ?
            """,
            (_now_utc(), local_path, post_id),
        )

    def mark_x_download_error(self, post_id: str, message: str) -> None:
        self._execute(
            "UPDATE x_posts SET last_error = ? WHERE post_id = ?",
            (str(message)[:500], post_id),
        )

    def recent_x_downloads(self, limit: int = 20) -> list[sqlite3.Row]:
        return self._query(
            """
            SELECT post_id, username, post_url, search_query, downloaded_at, local_path
            FROM x_posts WHERE downloaded = 1
            ORDER BY downloaded_at DESC LIMIT ?
            """,
            (max(1, min(100, int(limit))),),
        )

    # ------------------------------------------------------------------
    # Lebenszyklus
    # ------------------------------------------------------------------
    def backup(self, target: Path | str) -> Path:
        """Schreibt eine konsistente Sicherheitskopie der Datenbank.

        Nutzt die SQLite-eigene Backup-Schnittstelle – die Kopie ist auch
        dann in sich stimmig, wenn parallel geschrieben wird. Rückgabe:
        Pfad der erstellten Datei.
        """
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            dest = sqlite3.connect(target)
            try:
                self._conn.backup(dest)
            finally:
                dest.close()
        logger.info("Datenbank gesichert nach %s", target)
        return target

    def close(self) -> None:
        """Schließt die Datenbankverbindung."""
        with self._lock:
            self._conn.close()
        logger.debug("Datenbank geschlossen.")
