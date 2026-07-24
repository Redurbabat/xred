"""Tests für den atomaren Start des Massen-Download-Jobs.

Verhindert, dass zwei Auslöser (z. B. der Zeitplan um 12:00 UND ein Klick
auf „Herunterladen") gleichzeitig zwei Worker auf demselben Zustand starten.
"""

import threading

from app.web import _BulkJob


def test_try_start_sets_running_and_total():
    job = _BulkJob()
    assert job.snapshot()["running"] is False
    assert job.try_start(total=5, current="alice") is True
    snap = job.snapshot()
    assert snap["running"] is True
    assert snap["total"] == 5
    assert snap["current"] == "alice"


def test_try_start_rejects_second_start():
    job = _BulkJob()
    assert job.try_start(total=3) is True
    # Zweiter Start muss abgelehnt werden, solange der erste „läuft".
    assert job.try_start(total=99) is False
    assert job.snapshot()["total"] == 3   # unverändert


def test_try_start_is_atomic_under_contention():
    job = _BulkJob()
    ready = threading.Barrier(20)
    wins = []

    def contender():
        ready.wait()                 # alle Threads gleichzeitig loslassen
        if job.try_start(total=1):
            wins.append(1)

    threads = [threading.Thread(target=contender) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Trotz 20 gleichzeitiger Versuche darf GENAU einer gewinnen.
    assert sum(wins) == 1
    assert job.snapshot()["running"] is True
