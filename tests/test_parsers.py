from datetime import date

from jazz_calendar import (
    Event,
    calendar_text,
    deduplicate,
    parse_billetto_page,
    parse_fasching_page,
    parse_nefertiti_page,
    parse_playhouse_page,
    parse_unity_page,
)

TODAY = date(2026, 8, 23)


def test_fasching():
    html = """<html><h1>Harriet Tubman &amp; Georgia Anne Muldrow</h1><div>Datum tisdag 25 augusti 2026</div><div>Tider På scen: 20:00 Dörrarna öppnar: 18:00</div></html>"""
    e = parse_fasching_page(html, "https://www.fasching.se/test", TODAY)
    assert e and e.start.isoformat() == "2026-08-25T20:00:00"
    assert "18:00" in e.description


def test_nefertiti_infers_year():
    html = """<html><h1>Miriam Aïda</h1><h3>Fredag 11 september</h3><p>Datum: 11 sep Insläpp: 18:00 På Scen: 19:00 Stänger: 01:00</p></html>"""
    e = parse_nefertiti_page(html, "https://www.nefertiti.se/nefertiti_event/test/", TODAY)
    assert e and e.start.isoformat() == "2026-09-11T19:00:00"
    assert e.end.isoformat() == "2026-09-12T01:00:00"


def test_playhouse():
    html = """<html><h1>Hawk on flight</h1><p>Fusion – Fredag 9 Oktober 2026 - Playhouse Valand 18:00, på scen 19:00</p></html>"""
    e = parse_playhouse_page(html, "https://playhouse.nu/arrangemang/hawk/", TODAY)
    assert e and e.start.isoformat() == "2026-10-09T19:00:00"


def test_unity():
    html = """<html><h1>Jamnight</h1><p>torsdag 27 augusti 2026</p><p>19:00 23:00</p><p>Unity Jazz</p></html>"""
    e = parse_unity_page(html, "https://www.unityjazz.se/program/jam", TODAY)
    assert e and e.start.isoformat() == "2026-08-27T19:00:00"
    assert e.end.isoformat() == "2026-08-27T23:00:00"


def test_billetto():
    html = """<html><h1>The Beatles Goes Jazz</h1><p>Plats Utopia Jazz Karl Johansgatan 6, 414 59 Göteborg</p><p>Datum 2 apr 2026 20:00 - 23:00</p></html>"""
    e = parse_billetto_page(html, "https://billetto.se/e/test", TODAY)
    assert e and e.start.isoformat() == "2026-04-02T20:00:00"


def test_calendar_and_dedupe():
    e1 = Event("Test Band", __import__('datetime').datetime(2026, 9, 1, 19), __import__('datetime').datetime(2026, 9, 1, 22), "A", "X", "Göteborg", "One", "https://a")
    e2 = Event("Test Band!", __import__('datetime').datetime(2026, 9, 1, 19, 30), __import__('datetime').datetime(2026, 9, 1, 22), "B", "Y", "Göteborg", "Two", "https://b")
    out = deduplicate([e1, e2])
    assert len(out) == 1
    ics = calendar_text(out, "Test")
    assert "BEGIN:VCALENDAR" in ics and "BEGIN:VEVENT" in ics and "TZID=Europe/Stockholm" in ics
