"""
Gefolgt-/Follower-Listen aus einer Datei einlesen.
===================================================

Ziel: Du kannst deine EIGENE Liste (z. B. aus Instagrams offiziellem
Datenexport) als Datei hochladen; die App füllt daraus den lokalen
Zwischenspeicher. So erscheinen Gefolgt/Follower sofort – ganz ohne
Instagram-Abruf (praktisch, wenn Instagram gerade eine
Sicherheitsbestätigung verlangt).

Unterstützte Formate – in dieser Reihenfolge versucht:

  1. JSON  – Instagrams offizieller Export
             (``following.json`` / ``followers_1.json``): am saubersten.
  2. Text  – HTML-Export, CSV oder einfache Namensliste: es werden alle
             ``instagram.com/<name>``-Links bzw. sonst zeilenweise die
             Benutzernamen herausgezogen.
  3. PDF   – Notlösung; der Text wird extrahiert und wie unter 2.
             ausgewertet (erfordert das Paket ``pypdf``).

Rückgabe ist immer eine Liste ``[{"username", "full_name", "pic"}, …]``
in der Reihenfolge des Auftretens, ohne Duplikate.
"""

from __future__ import annotations

import json
import logging
import re

from .util import _RESERVED_SEGMENTS, USERNAME_RE

logger = logging.getLogger(__name__)

# instagram.com/<name> aus HTML/Text (ohne Beitrags-/Reel-Links).
_URL_RE = re.compile(r"instagram\.com/([A-Za-z0-9._]+)", re.IGNORECASE)


class ImportError_(Exception):
    """Die Datei konnte nicht als Liste gelesen werden."""


def _dedupe(usernames: list[str]) -> list[dict]:
    """Wandelt Rohnamen in die Zwischenspeicher-Struktur (ohne Duplikate)."""
    seen: set[str] = set()
    out: list[dict] = []
    for name in usernames:
        name = (name or "").strip().lstrip("@").lower()
        if not name or name in _RESERVED_SEGMENTS or not USERNAME_RE.match(name):
            continue
        if name in seen:
            continue
        seen.add(name)
        out.append({"username": name, "full_name": "", "pic": ""})
    return out


def _username_from_href(href: str) -> str:
    """Zieht den Benutzernamen aus einer Instagram-Profil-URL.

    Behandelt auch die „In App öffnen“-Form ``instagram.com/_u/<name>``.
    """
    m = _URL_RE.search(href or "")
    if not m:
        return ""
    seg = m.group(1)
    if seg == "_u":
        # Nächstes Pfadsegment nach /_u/ ist der eigentliche Name.
        rest = href.split("/_u/", 1)
        if len(rest) == 2:
            return rest[1].split("/")[0].split("?")[0]
        return ""
    return seg


def _from_instagram_json(data) -> list[str]:
    """Zieht Namen aus Instagrams offiziellem JSON-Export.

    Deckt beide bekannten Formen ab:
      * Follower-Datei: ``[ {"string_list_data":[{"value": …}]} … ]``
      * Gefolgt-Datei:  ``{"relationships_following": [ {"title": "<name>",
        "string_list_data":[{"href": ".../_u/<name>"}]} … ]}`` – hier steht
        der Name im Feld ``title`` bzw. in der ``href``-URL, NICHT in
        ``value``.
    """
    # Die eigentliche Liste finden.
    entries = None
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        for key, value in data.items():
            if key.startswith("relationships_") and isinstance(value, list):
                entries = value
                break
        if entries is None:
            # Manche Exporte kapseln die Liste unter einem beliebigen Schlüssel.
            for value in data.values():
                if isinstance(value, list):
                    entries = value
                    break
    if entries is None:
        return []

    names: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        found = False
        sld = entry.get("string_list_data")
        if isinstance(sld, list):
            for item in sld:
                if not isinstance(item, dict):
                    continue
                if item.get("value"):
                    names.append(str(item["value"]))
                    found = True
                elif item.get("href"):
                    name = _username_from_href(str(item["href"]))
                    if name:
                        names.append(name)
                        found = True
        if not found and entry.get("value"):
            names.append(str(entry["value"]))
            found = True
        # Gefolgt-Datei: Name steht im Feld "title".
        if not found and entry.get("title"):
            names.append(str(entry["title"]))
    return names


def _from_text(text: str) -> list[str]:
    """Zieht Benutzernamen aus HTML/CSV/Textliste.

    Bevorzugt ``instagram.com/<name>``-Links (HTML-Export). Findet es keine,
    wird jede Zeile als möglicher Benutzername gewertet (einfache Liste,
    eine pro Zeile oder komma-/leerzeichengetrennt).
    """
    links = [m.group(1) for m in _URL_RE.finditer(text)]
    if links:
        return links
    names: list[str] = []
    for line in text.splitlines():
        for token in re.split(r"[,;\s]+", line.strip()):
            token = token.strip().lstrip("@")
            if token:
                names.append(token)
    return names


def _pdf_to_text(raw: bytes) -> str:
    """Extrahiert den Text aus einem PDF (benötigt ``pypdf``)."""
    try:
        import io

        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - abhängig von Installation
        raise ImportError_(
            "Für PDF-Dateien wird das Paket „pypdf“ benötigt. Bitte die App "
            "einmal über start-web.bat neu starten (dann wird es automatisch "
            "installiert) – oder die Liste als TXT/JSON hochladen."
        ) from exc
    try:
        reader = PdfReader(io.BytesIO(raw))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:
        raise ImportError_(f"PDF konnte nicht gelesen werden: {exc}") from exc


def parse_account_file(raw: bytes, filename: str = "") -> list[dict]:
    """Liest eine hochgeladene Datei und liefert die Konten-Liste.

    Wirft :class:`ImportError_` mit einer verständlichen Meldung, wenn sich
    keine Namen finden lassen.
    """
    name = (filename or "").lower()

    # 1. PDF an der Endung erkennen (Text extrahieren, dann wie Text behandeln).
    if name.endswith(".pdf") or raw[:5] == b"%PDF-":
        usernames = _from_text(_pdf_to_text(raw))
        accounts = _dedupe(usernames)
        if not accounts:
            raise ImportError_(
                "Im PDF wurden keine Benutzernamen gefunden. Tipp: Instagrams "
                "offizieller Datenexport (JSON) klappt am zuverlässigsten."
            )
        return accounts

    # 2. Als Text dekodieren (für JSON und alle Textformate).
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", errors="replace")

    # 3. JSON versuchen (Instagram-Export).
    usernames: list[str] = []
    stripped = text.lstrip()
    if name.endswith(".json") or stripped[:1] in "{[":
        try:
            usernames = _from_instagram_json(json.loads(text))
        except (json.JSONDecodeError, ValueError):
            usernames = []

    # 4. Sonst / falls JSON nichts ergab: als Text auswerten.
    if not usernames:
        usernames = _from_text(text)

    accounts = _dedupe(usernames)
    if not accounts:
        raise ImportError_(
            "In der Datei wurden keine Benutzernamen gefunden. Unterstützt "
            "werden: Instagram-Datenexport (JSON/HTML), CSV oder eine einfache "
            "Namensliste (ein Name pro Zeile)."
        )
    return accounts


def parse_export_zip(raw: bytes) -> dict[str, list[dict]]:
    """Liest Instagrams komplettes Export-ZIP: beide Listen in einem Rutsch.

    Sucht im Archiv nach ``following.json`` und allen ``followers_*.json``
    (liegen unter ``connections/followers_and_following/``) und wertet sie
    mit :func:`parse_account_file` aus. Ergebnis:
    ``{"following": [...], "followers": [...]}`` – Listen können leer sein.
    """
    import io
    import zipfile

    result: dict[str, list[dict]] = {"following": [], "followers": []}
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            for name in z.namelist():
                base = name.rsplit("/", 1)[-1].lower()
                if not base.endswith(".json"):
                    continue
                try:
                    if base == "following.json":
                        result["following"].extend(
                            parse_account_file(z.read(name), base))
                    elif base.startswith("followers_"):
                        result["followers"].extend(
                            parse_account_file(z.read(name), base))
                except ImportError_:
                    continue  # einzelne unpassende Datei überspringen
    except Exception as exc:
        raise ImportError_(f"ZIP konnte nicht gelesen werden: {exc}") from None

    # Duplikate (z. B. über mehrere followers_N.json) entfernen.
    for kind, lst in result.items():
        seen: set[str] = set()
        unique: list[dict] = []
        for account in lst:
            if account["username"] in seen:
                continue
            seen.add(account["username"])
            unique.append(account)
        result[kind] = unique
    return result
