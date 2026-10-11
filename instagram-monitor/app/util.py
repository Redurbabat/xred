"""
Gemeinsame Hilfsfunktionen für Desktop-GUI und Web-Oberfläche.
"""

from __future__ import annotations

import re

# Gültige Instagram-Benutzernamen: Buchstaben, Ziffern, Punkt, Unterstrich,
# maximal 30 Zeichen.
USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")

# Benutzername aus einer kompletten Instagram-Adresse herausziehen,
# z. B. https://www.instagram.com/nasa/ → nasa
_URL_RE = re.compile(r"instagram\.com/([A-Za-z0-9._]+)", re.IGNORECASE)

# Pfad-Segmente von instagram.com, die KEINE Profilnamen sind
# (Beitrags-/Reel-/Story-Links usw.).
_RESERVED_SEGMENTS = {"p", "reel", "reels", "tv", "stories", "explore", "accounts"}

# Beitrags-Kürzel (Shortcode) aus einem Beitrags-/Reel-Link ziehen,
# z. B. https://www.instagram.com/p/ABC123/ oder /reel/XYZ/?igsh=… → ABC123.
_SHORTCODE_RE = re.compile(
    r"instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", re.IGNORECASE
)


def extract_username(raw: str) -> str:
    """Normalisiert eine Eingabe zu einem Benutzernamen.

    Akzeptiert neben dem reinen Namen auch "@name" und komplette
    Instagram-Adressen. Liefert "" für Eingaben, aus denen sich kein
    Profilname ableiten lässt (z. B. ein Link auf einen einzelnen
    Beitrag statt auf ein Profil).
    """
    raw = raw.strip()
    match = _URL_RE.search(raw)
    if match:
        segment = match.group(1)
        if segment.lower() in _RESERVED_SEGMENTS:
            return ""  # Beitrags-/Reel-Link, kein Profil-Link
        raw = segment
    return raw.lstrip("@").lower()


def extract_shortcode(raw: str) -> str:
    """Zieht das Beitrags-Kürzel aus einem Instagram-Link (auch aus Freitext).

    Erkennt Beitrags-, Reel- und IGTV-Links – auch mitten in geteiltem Text
    mit weiteren Zeichen/Query-Parametern (z. B. „Schau dir das an:
    https://www.instagram.com/reel/XYZ/?igsh=…"). Liefert "" wenn kein
    solcher Link enthalten ist (z. B. ein Profil- oder Story-Link).
    """
    match = _SHORTCODE_RE.search(raw or "")
    return match.group(1) if match else ""
