"""Wächst über die mitgelieferte Startliste (app/seed_lists.json).

Stellt sicher, dass die Datei – falls vorhanden – das erwartete Format hat
(Dict mit ``following``/``followers`` als Listen nicht-leerer Benutzernamen),
damit eine kaputte Startliste nicht erst beim App-Start auffällt. Zählt
bewusst KEINE konkreten Namen/Mengen (die dürfen sich jederzeit ändern).
"""

import json
from pathlib import Path

SEED = Path(__file__).resolve().parent.parent / "app" / "seed_lists.json"


def test_seed_lists_is_well_formed():
    if not SEED.is_file():
        return  # Startliste ist optional – ohne sie startet die App auch.
    data = json.loads(SEED.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert set(data).issubset({"following", "followers"})
    for kind in ("following", "followers"):
        names = data.get(kind, [])
        assert isinstance(names, list)
        assert all(isinstance(n, str) and n.strip() for n in names)
