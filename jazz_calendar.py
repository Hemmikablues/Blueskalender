#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import html as html_lib
import json
import logging
import os
import re
import sys
import time as time_module
import unicodedata
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

TZID = "Europe/Stockholm"
CALENDAR_VERSION = "2026-08-23-web-v7-musikkalender"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151 Safari/537.36 "
    "JazzCalendarPrototype/0.1"
)

SOURCES = {
    "Fasching": "https://www.fasching.se/kalendarium/",
    "Nefertiti": "https://www.nefertiti.se/kalendarium/",
    "Playhouse": "https://playhouse.nu/program/",
    "Skeppet GBG": "https://www.skeppetgbg.se/?post_type=tribe_events",
    "Unity Jazz": "https://www.unityjazz.se/program",
    "Musikens Hus & Hängmattan": "https://www.musikenshus.se/kalender/",
    "Utopia Jazz": "https://billetto.se/users/utopia-jazz",
}

MONTHS = {
    "januari": 1, "jan": 1,
    "februari": 2, "feb": 2,
    "mars": 3, "mar": 3,
    "april": 4, "apr": 4,
    "maj": 5,
    "juni": 6, "jun": 6,
    "juli": 7, "jul": 7,
    "augusti": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "oktober": 10, "okt": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

WEEKDAY_RE = r"(?:måndag|tisdag|onsdag|torsdag|fredag|lördag|söndag|mån|tis|ons|tors|tor|fre|lör|sön)"
MONTH_RE = "(?:" + "|".join(sorted(MONTHS, key=len, reverse=True)) + ")"


class SourceError(RuntimeError):
    pass


@dataclass
class Event:
    title: str
    start: datetime
    end: datetime
    venue: str
    address: str
    city: str
    source: str
    url: str
    description: str = ""
    category: str = ""

    @property
    def uid(self) -> str:
        raw = f"{self.source}|{self.url}|{self.title}|{self.start.isoformat()}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest() + "@jazzkalender.local"


@dataclass
class SourceStatus:
    source: str
    ok: bool
    count: int
    latest: str = ""
    message: str = ""
    using_fallback: bool = False


def make_session() -> requests.Session:
    s = requests.Session()
    retries = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
    )
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/json,text/calendar;q=0.9,*/*;q=0.8",
        "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.7",
    })
    return s


def fetch(session: requests.Session, url: str, *, timeout: int = 25) -> requests.Response:
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    text = r.text[:5000].lower()
    blocked = (
        "please wait while your request is being verified" in text
        or "just a moment" in text and "cloudflare" in text
        or "cf-chl-" in text
    )
    if blocked:
        raise SourceError(f"Bot-skydd blockerade hämtningen: {url}")
    return r


# Musikens Hus svarar periodvis inte alls på anslutningar från GitHubs runners.
# V7 använder därför INGA publika proxyer. I stället görs flera direkta försök
# och senaste lyckade Musikens Hus-data sparas separat på GitHub Pages.
# Vid ett tillfälligt fel används den senast kända källcachen och den skrivs
# tillbaka vid publicering, så att en misslyckad körning inte kan radera den.
_MH_TRANSPORT_FALLBACK = False


def fetch_musikens_hus_index(session: requests.Session, target_url: str) -> str:
    global _MH_TRANSPORT_FALLBACK
    _MH_TRANSPORT_FALLBACK = False
    candidates = (
        target_url,
        "https://www.musikenshus.se/",
        "https://musikenshus.se/kalender/",
    )
    errors: list[str] = []
    # Två omgångar ger servern en ny chans utan att en körning blir orimligt lång.
    for round_no in range(2):
        for candidate in candidates:
            try:
                html = fetch(session, candidate, timeout=12).text
                low = html[:20000].lower()
                if "hängmattan" not in low and "musikens hus" not in low:
                    raise SourceError("Svaret ser inte ut som Musikens Hus kalender.")
                return html
            except Exception as exc:
                errors.append(f"{candidate}: {exc}")
        if round_no == 0:
            logging.warning("Musikens Hus svarade inte. Väntar 30 sekunder och försöker igen.")
            time_module.sleep(30)
    raise SourceError("Direkthämtning från Musikens Hus misslyckades efter upprepade försök: " + " | ".join(errors[-6:]))


def clean_text(value: str) -> str:
    value = html_lib.unescape(value or "")
    value = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", value).strip()


def soup_text(soup: BeautifulSoup) -> str:
    return clean_text(soup.get_text(" ", strip=True))


def set_query(url: str, **kwargs: str) -> str:
    p = urlparse(url)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    q.update(kwargs)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(q), p.fragment))


def infer_year(day: int, month: int, today: date) -> int:
    candidate = date(today.year, month, day)
    if candidate < today - timedelta(days=21):
        return today.year + 1
    return today.year


def parse_swedish_date(text: str, today: date | None = None) -> date | None:
    today = today or date.today()
    t = clean_text(text).lower().replace(".", "")
    patterns = [
        rf"{WEEKDAY_RE}\s+(\d{{1,2}})\s+({MONTH_RE})(?:\s+(\d{{4}}))?",
        rf"(?:datum\s*:?\s*)?(\d{{1,2}})\s+({MONTH_RE})(?:\s+(\d{{4}}))?",
    ]
    for pat in patterns:
        m = re.search(pat, t, re.I)
        if m:
            day = int(m.group(1))
            month = MONTHS[m.group(2).lower()]
            year = int(m.group(3)) if m.group(3) else infer_year(day, month, today)
            try:
                return date(year, month, day)
            except ValueError:
                return None
    return None


def parse_hhmm(text: str) -> time | None:
    m = re.search(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b", text)
    if not m:
        return None
    return time(int(m.group(1)), int(m.group(2)))


def local_dt(d: date, t: time) -> datetime:
    return datetime.combine(d, t)


def sensible_end(start: datetime, end_time: time | None = None, hours: int = 3) -> datetime:
    if end_time is None:
        return start + timedelta(hours=hours)
    end = datetime.combine(start.date(), end_time)
    if end <= start:
        end += timedelta(days=1)
    return end


def strip_html(value: str) -> str:
    return clean_text(BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True))


def title_from_soup(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")
    if h1:
        return clean_text(h1.get_text(" ", strip=True))
    if soup.title:
        return clean_text(soup.title.get_text(" ", strip=True)).split(" | ")[0]
    return "Evenemang"


def iter_json_objects(obj: Any) -> Iterable[dict[str, Any]]:
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from iter_json_objects(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from iter_json_objects(value)


def parse_jsonld_event(soup: BeautifulSoup, source: str, fallback_url: str) -> Event | None:
    for script in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = script.string or script.get_text()
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for obj in iter_json_objects(data):
            typ = obj.get("@type")
            types = typ if isinstance(typ, list) else [typ]
            if not any(str(x).lower() == "event" for x in types if x):
                continue
            name = clean_text(str(obj.get("name", "")))
            start_raw = obj.get("startDate")
            if not name or not start_raw:
                continue
            try:
                start = dtparser.isoparse(str(start_raw)).replace(tzinfo=None)
                end_raw = obj.get("endDate")
                end = dtparser.isoparse(str(end_raw)).replace(tzinfo=None) if end_raw else start + timedelta(hours=3)
            except Exception:
                continue
            location = obj.get("location") or {}
            if isinstance(location, list):
                location = location[0] if location else {}
            venue = clean_text(str(location.get("name", ""))) if isinstance(location, dict) else ""
            addr = location.get("address", {}) if isinstance(location, dict) else {}
            if isinstance(addr, str):
                address = clean_text(addr)
                city = ""
            else:
                address = clean_text(" ".join(str(addr.get(k, "")) for k in ("streetAddress", "postalCode") if addr.get(k)))
                city = clean_text(str(addr.get("addressLocality", "")))
            return Event(
                title=name,
                start=start,
                end=end,
                venue=venue,
                address=address,
                city=city,
                source=source,
                url=str(obj.get("url") or fallback_url),
                description=strip_html(str(obj.get("description", ""))),
            )
    return None


def event_links(soup: BeautifulSoup, base: str, predicate) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"])
        if predicate(href, a):
            href = href.split("#", 1)[0]
            if href not in seen:
                found.append(href)
                seen.add(href)
    return found


def parse_fasching_page(html: str, url: str, today: date | None = None) -> Event | None:
    soup = BeautifulSoup(html, "html.parser")
    structured = parse_jsonld_event(soup, "Fasching", url)
    if structured:
        structured.venue = structured.venue or "Fasching"
        structured.city = structured.city or "Stockholm"
        structured.address = structured.address or "Kungsgatan 63"
        return structured
    text = soup_text(soup)
    d = parse_swedish_date(text, today)
    m = re.search(r"På scen\s*:?\s*([0-2]?\d[:.]\d{2})", text, re.I)
    if not d or not m:
        return None
    start_t = parse_hhmm(m.group(1))
    if not start_t:
        return None
    doors = re.search(r"Dörrarna öppnar\s*:?\s*([0-2]?\d[:.]\d{2})", text, re.I)
    desc = f"Dörrarna öppnar {doors.group(1).replace('.', ':')}." if doors else ""
    start = local_dt(d, start_t)
    return Event(title_from_soup(soup), start, sensible_end(start), "Fasching", "Kungsgatan 63", "Stockholm", "Fasching", url, desc)


def parse_fasching_events_page(html: str, url: str, today: date | None = None) -> list[Event]:
    """Parse one Fasching page, including pages that contain several performance dates."""
    today = today or date.today()
    soup = BeautifulSoup(html, "html.parser")
    text = soup_text(soup)
    title = title_from_soup(soup)
    events: list[Event] = []

    # Fasching sometimes puts several dates on one artist page. Parse every Datum/Tider block.
    starts = [m.start() for m in re.finditer(r"\bDatum\s+", text, re.I)]
    if starts:
        starts.append(len(text))
        for i in range(len(starts) - 1):
            block = text[starts[i]:starts[i + 1]]
            d = parse_swedish_date(block, today)
            tm = re.search(r"På scen\s*:?\s*([0-2]?\d[:.]\d{2})", block, re.I)
            if not d or not tm:
                continue
            start_t = parse_hhmm(tm.group(1))
            if not start_t:
                continue
            doors = re.search(r"Dörrarna öppnar\s*:?\s*([0-2]?\d[:.]\d{2})", block, re.I)
            desc = f"Dörrarna öppnar {doors.group(1).replace('.', ':')}." if doors else ""
            start = local_dt(d, start_t)
            events.append(Event(title, start, sensible_end(start), "Fasching", "Kungsgatan 63", "Stockholm", "Fasching", url, desc))
    if events:
        return events

    ev = parse_fasching_page(html, url, today)
    return [ev] if ev else []


def scrape_fasching(session: requests.Session, today: date) -> list[Event]:
    # The main kalendarium is dynamically loaded and can expose only the first few shows
    # to a non-browser client. The stage pages contain the full future programme.
    index_urls = [
        "https://www.fasching.se/scen/stora-scen/",
        "https://www.fasching.se/scen/foajebaren/",
        SOURCES["Fasching"],
    ]
    links: list[str] = []
    for index_url in index_urls:
        try:
            soup = BeautifulSoup(fetch(session, index_url).text, "html.parser")
        except Exception as exc:
            logging.debug("Fasching index failed %s: %s", index_url, exc)
            continue

        def pred(href: str, a) -> bool:
            p = urlparse(href)
            if p.netloc not in ("www.fasching.se", "fasching.se"):
                return False
            if p.path.startswith("/en/") or p.path.startswith("/scen/") or p.path == "/kalendarium/":
                return False
            if re.search(r"-20\d{2}-\d{2}-\d{2}/?$", p.path):
                return True
            ancestor = a
            for _ in range(5):
                ancestor = ancestor.parent
                if ancestor is None:
                    break
                txt = clean_text(ancestor.get_text(" ", strip=True)).lower()
                if re.search(rf"\b{WEEKDAY_RE}\s+\d{{1,2}}\s+{MONTH_RE}\b", txt, re.I):
                    return p.path.count("/") <= 2 and p.path not in ("/", "/kalendarium/")
            return False

        for link in event_links(soup, index_url, pred):
            if link not in links:
                links.append(link)

    if not links:
        raise SourceError("Fasching gav inga evenemangslänkar från scen- eller kalendariumsidorna.")
    events: list[Event] = []
    for link in links[:260]:
        try:
            events.extend(parse_fasching_events_page(fetch(session, link).text, link, today))
        except Exception as exc:
            logging.debug("Fasching event failed %s: %s", link, exc)
    return deduplicate(events)


def parse_nefertiti_page(html: str, url: str, today: date | None = None) -> Event | None:
    soup = BeautifulSoup(html, "html.parser")
    structured = parse_jsonld_event(soup, "Nefertiti", url)
    if structured:
        structured.venue = structured.venue or "Nefertiti"
        structured.city = structured.city or "Göteborg"
        structured.address = structured.address or "Hvitfeldtsplatsen 6"
        return structured
    text = soup_text(soup)
    d = parse_swedish_date(text, today)
    m = re.search(r"På\s*Scen\s*:?\s*([0-2]?\d[:.]\d{2})", text, re.I)
    if not d or not m:
        return None
    start_t = parse_hhmm(m.group(1))
    if not start_t:
        return None
    end_m = re.search(r"Stänger\s*:?\s*([0-2]?\d[:.]\d{2})", text, re.I)
    end_t = parse_hhmm(end_m.group(1)) if end_m else None
    doors = re.search(r"Insläpp\s*:?\s*([0-2]?\d[:.]\d{2})", text, re.I)
    desc = f"Insläpp {doors.group(1).replace('.', ':')}." if doors else ""
    start = local_dt(d, start_t)
    return Event(title_from_soup(soup), start, sensible_end(start, end_t), "Nefertiti", "Hvitfeldtsplatsen 6", "Göteborg", "Nefertiti", url, desc)


def nefertiti_listing_fallback(soup: BeautifulSoup, base: str, today: date) -> dict[str, Event]:
    """Build date/title fallback records from the calendar listing if detail pages are bot-blocked."""
    fallback: dict[str, Event] = {}
    for a in soup.find_all("a", href=True):
        link = urljoin(base, a["href"]).split("#", 1)[0]
        if "/nefertiti_event/" not in urlparse(link).path:
            continue
        title = clean_text(a.get_text(" ", strip=True))
        if not title or title.lower() in ("läs mer", "kop biljett", "köp biljett"):
            continue
        d = None
        ancestor = a
        for _ in range(5):
            ancestor = ancestor.parent
            if ancestor is None:
                break
            d = parse_swedish_date(clean_text(ancestor.get_text(" ", strip=True)), today)
            if d:
                break
        if not d:
            continue
        start = local_dt(d, time(19, 0))
        fallback[link] = Event(
            title, start, sensible_end(start, time(23, 0)),
            "Nefertiti", "Hvitfeldtsplatsen 6", "Göteborg", "Nefertiti", link,
            "Reservpost från kalendariet. Scenstart kunde inte verifieras på detaljsidan; 19:00 används som reservtid."
        )
    return fallback


def scrape_nefertiti(session: requests.Session, today: date) -> list[Event]:
    url = SOURCES["Nefertiti"]
    soup = BeautifulSoup(fetch(session, url).text, "html.parser")
    links = event_links(soup, url, lambda href, a: "/nefertiti_event/" in urlparse(href).path)
    fallback = nefertiti_listing_fallback(soup, url, today)
    events: list[Event] = []
    for link in links[:220]:
        try:
            ev = parse_nefertiti_page(fetch(session, link).text, link, today)
            if ev:
                events.append(ev)
                continue
        except Exception as exc:
            logging.debug("Nefertiti detail failed %s: %s", link, exc)
        if link in fallback:
            events.append(fallback[link])
    if not events and fallback:
        events.extend(fallback.values())
    return deduplicate(events)


def parse_playhouse_page(html: str, url: str, today: date | None = None) -> Event | None:
    soup = BeautifulSoup(html, "html.parser")
    structured = parse_jsonld_event(soup, "Playhouse", url)
    if structured:
        structured.venue = structured.venue or "Playhouse Valand"
        structured.city = structured.city or "Göteborg"
        structured.address = structured.address or "Vasagatan 41"
        return structured
    text = soup_text(soup)
    d = parse_swedish_date(text, today)
    m = re.search(r"på scen\s*([0-2]?\d[:.]\d{2})", text, re.I)
    if not d or not m:
        return None
    start_t = parse_hhmm(m.group(1))
    if not start_t:
        return None
    start = local_dt(d, start_t)
    meta = re.search(r"([0-2]?\d[:.]\d{2})\s*,\s*på scen", text, re.I)
    desc = f"Öppnar {meta.group(1).replace('.', ':')}." if meta else ""
    return Event(title_from_soup(soup), start, sensible_end(start), "Playhouse Valand", "Vasagatan 41", "Göteborg", "Playhouse", url, desc)


def scrape_playhouse(session: requests.Session, today: date) -> list[Event]:
    url = SOURCES["Playhouse"]
    soup = BeautifulSoup(fetch(session, url).text, "html.parser")
    links = event_links(soup, url, lambda href, a: "/arrangemang/" in urlparse(href).path)
    events = []
    for link in links[:120]:
        try:
            ev = parse_playhouse_page(fetch(session, link).text, link, today)
            if ev:
                events.append(ev)
        except Exception as exc:
            logging.debug("Playhouse event failed %s: %s", link, exc)
    return events


def parse_skeppet_rest(data: dict[str, Any]) -> list[Event]:
    events: list[Event] = []
    for obj in data.get("events", []):
        try:
            start = dtparser.parse(obj["start_date"]).replace(tzinfo=None)
            end = dtparser.parse(obj.get("end_date") or obj["start_date"]).replace(tzinfo=None)
        except Exception:
            continue
        venue_obj = obj.get("venue") or {}
        address = clean_text(" ".join(str(venue_obj.get(k, "")) for k in ("address", "zip") if venue_obj.get(k)))
        events.append(Event(
            title=strip_html(obj.get("title", "Evenemang")),
            start=start,
            end=end if end > start else start + timedelta(hours=3),
            venue=strip_html(venue_obj.get("venue", "Skeppet GBG")) or "Skeppet GBG",
            address=address or "Amerikagatan 2",
            city=clean_text(str(venue_obj.get("city", "Göteborg"))) or "Göteborg",
            source="Skeppet GBG",
            url=str(obj.get("url") or SOURCES["Skeppet GBG"]),
            description=strip_html(obj.get("description", "")),
        ))
    return events


def unfold_ics(text: str) -> list[str]:
    raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    for line in raw:
        if line.startswith((" ", "\t")) and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def unescape_ics(value: str) -> str:
    return value.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def parse_ics_datetime(value: str) -> datetime:
    value = value.strip()
    if re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value, "%Y%m%d")
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ")
    return datetime.strptime(value[:15], "%Y%m%dT%H%M%S")


def parse_ics_events(text: str, source: str, default_venue: str, default_city: str, default_address: str) -> list[Event]:
    lines = unfold_ics(text)
    chunks: list[list[str]] = []
    current: list[str] | None = None
    for line in lines:
        if line == "BEGIN:VEVENT":
            current = []
        elif line == "END:VEVENT" and current is not None:
            chunks.append(current)
            current = None
        elif current is not None:
            current.append(line)
    out: list[Event] = []
    for chunk in chunks:
        props: dict[str, str] = {}
        for line in chunk:
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            props[k.split(";", 1)[0].upper()] = v
        if "DTSTART" not in props or "SUMMARY" not in props:
            continue
        try:
            start = parse_ics_datetime(props["DTSTART"])
            end = parse_ics_datetime(props["DTEND"]) if "DTEND" in props else start + timedelta(hours=3)
        except Exception:
            continue
        location = unescape_ics(props.get("LOCATION", ""))
        out.append(Event(
            title=clean_text(unescape_ics(props["SUMMARY"])),
            start=start,
            end=end if end > start else start + timedelta(hours=3),
            venue=default_venue,
            address=location or default_address,
            city=default_city,
            source=source,
            url=unescape_ics(props.get("URL", "")) or SOURCES.get(source, ""),
            description=clean_text(unescape_ics(props.get("DESCRIPTION", ""))),
        ))
    return out


def scrape_skeppet(session: requests.Session, today: date) -> list[Event]:
    rest = "https://www.skeppetgbg.se/wp-json/tribe/events/v1/events"
    try:
        r = session.get(rest, params={"per_page": 100, "start_date": today.isoformat()}, timeout=25)
        if r.ok and "application/json" in r.headers.get("Content-Type", ""):
            events = parse_skeppet_rest(r.json())
            if events:
                return events
    except Exception as exc:
        logging.debug("Skeppet REST failed: %s", exc)

    candidates = [
        "https://www.skeppetgbg.se/events/list/?ical=1",
        "https://www.skeppetgbg.se/events/?ical=1",
        "https://www.skeppetgbg.se/?post_type=tribe_events&ical=1",
    ]
    for url in candidates:
        try:
            r = session.get(url, timeout=25)
            if r.ok and "BEGIN:VCALENDAR" in r.text:
                events = parse_ics_events(r.text, "Skeppet GBG", "Skeppet GBG", "Göteborg", "Amerikagatan 2")
                if events:
                    return events
        except Exception as exc:
            logging.debug("Skeppet ICS failed %s: %s", url, exc)
    raise SourceError("Varken Skeppets REST-gränssnitt eller iCalendar-flöde kunde läsas.")


def parse_unity_page(html: str, url: str, today: date | None = None) -> Event | None:
    soup = BeautifulSoup(html, "html.parser")
    structured = parse_jsonld_event(soup, "Unity Jazz", url)
    if structured:
        structured.venue = structured.venue or "Unity Jazz"
        structured.city = structured.city or "Göteborg"
        structured.address = structured.address or "Kyrkogatan 13"
        return structured
    text = soup_text(soup)
    d = parse_swedish_date(text, today)
    if not d:
        return None
    times = re.findall(r"\b([0-2]?\d[:.]\d{2})\b", text)
    if len(times) < 2:
        return None
    start_t, end_t = parse_hhmm(times[0]), parse_hhmm(times[1])
    if not start_t:
        return None
    start = local_dt(d, start_t)
    return Event(title_from_soup(soup), start, sensible_end(start, end_t), "Unity Jazz", "Kyrkogatan 13", "Göteborg", "Unity Jazz", url)


def scrape_unity(session: requests.Session, today: date) -> list[Event]:
    url = SOURCES["Unity Jazz"]
    soup = BeautifulSoup(fetch(session, url).text, "html.parser")
    links = event_links(soup, url, lambda href, a: urlparse(href).path.startswith("/program/") and urlparse(href).path != "/program/")
    events: list[Event] = []
    for link in links[:160]:
        try:
            ics_url = set_query(link, format="ical")
            r = session.get(ics_url, timeout=20)
            if r.ok and "BEGIN:VCALENDAR" in r.text:
                parsed = parse_ics_events(r.text, "Unity Jazz", "Unity Jazz", "Göteborg", "Kyrkogatan 13")
                if parsed:
                    for ev in parsed:
                        ev.url = link
                    events.extend(parsed)
                    continue
            ev = parse_unity_page(fetch(session, link).text, link, today)
            if ev:
                events.append(ev)
        except Exception as exc:
            logging.debug("Unity event failed %s: %s", link, exc)
    return events


def extract_billetto_links(html: str, base: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = event_links(soup, base, lambda href, a: urlparse(href).netloc.endswith("billetto.se") and "/e/" in urlparse(href).path)
    raw_candidates = re.findall(r"(?:https?:\\?/\\?/billetto\.se)?\\?/e\\?/[^\"'<>\\s]+", html, flags=re.I)
    for raw in raw_candidates:
        raw = raw.replace("\\/", "/")
        if raw.startswith("/e/"):
            raw = urljoin("https://billetto.se", raw)
        elif raw.startswith("e/"):
            raw = urljoin("https://billetto.se/", raw)
        if raw.startswith("http") and raw not in links:
            links.append(raw.split("?", 1)[0])
    return links


def parse_musikens_hus_page(html: str, url: str, today: date | None = None) -> Event | None:
    soup = BeautifulSoup(html, "html.parser")
    structured = parse_jsonld_event(soup, "Musikens Hus & Hängmattan", url)
    if structured:
        venue_l = (structured.venue or "").lower()
        if "hängmattan" in venue_l and ("stora" in venue_l or "musikens hus" in venue_l):
            structured.venue = structured.venue or "Musikens Hus & Hängmattan"
            structured.address = structured.address or "Djurgårdsgatan 13 / Karl Johansgatan 16"
            structured.source = "Musikens Hus & Hängmattan"
        elif "hängmattan" in venue_l:
            structured.venue = structured.venue or "Hängmattan"
            structured.address = structured.address or "Karl Johansgatan 16"
            structured.source = "Hängmattan"
        else:
            structured.venue = structured.venue or "Musikens Hus"
            structured.address = structured.address or "Djurgårdsgatan 13"
            structured.source = "Musikens Hus"
        structured.city = structured.city or "Göteborg"
        return structured

    text = soup_text(soup)
    d = parse_swedish_date(text, today)
    if not d:
        return None

    # Prefer actual performance/start time, then opening time. Some pages only list "Öppet 22.00-02.00".
    time_patterns = [
        r"På\s+scen(?:\s+ca)?\s+kl\.?\s*([0-2]?\d[:.]\d{2})",
        r"Start\s+kl\.?\s*([0-2]?\d[:.]\d{2})",
        r"Öppet\s*([0-2]?\d[:.]\d{2})\s*[-–]\s*([0-2]?\d[:.]\d{2})",
        r"Vi\s+öppnar\s+kl\.?\s*([0-2]?\d[:.]\d{2})",
    ]
    start_t = None
    end_t = None
    for i, pat in enumerate(time_patterns):
        m = re.search(pat, text, re.I)
        if not m:
            continue
        start_t = parse_hhmm(m.group(1))
        if i == 2 and m.lastindex and m.lastindex >= 2:
            end_t = parse_hhmm(m.group(2))
        if start_t:
            break
    if not start_t:
        # Keep the event even when the organiser has not announced a time yet.
        start_t = time(19, 0)
        time_note = "Starttid saknas/TBA på källsidan; 19:00 används som reservtid."
    else:
        time_note = ""

    # If a separate end time is stated, use it.
    if end_t is None:
        end_m = re.search(r"(?:Slut|Stänger)\s*(?:kl\.?)?\s*([0-2]?\d[:.]\d{2})", text, re.I)
        end_t = parse_hhmm(end_m.group(1)) if end_m else None

    # Detect the venue immediately after the full event date. This avoids menu/footer text
    # elsewhere on the page causing a false venue match.
    venue = "Musikens Hus"
    address = "Djurgårdsgatan 13"
    venue_match = re.search(
        rf"{WEEKDAY_RE}\s+\d{{1,2}}\s+{MONTH_RE}\s+\d{{4}}\s+"
        r"(Stora\s+scen(?:en)?\s*&\s*Hängmattan(?:\s+Scen)?|Stora\s+scen(?:en)?|Hängmattan(?:\s+Scen)?)",
        text, re.I,
    )
    venue_text = clean_text(venue_match.group(1)).lower() if venue_match else ""
    if "stora" in venue_text and "hängmattan" in venue_text:
        venue = "Musikens Hus & Hängmattan"
        address = "Djurgårdsgatan 13 / Karl Johansgatan 16"
    elif "hängmattan" in venue_text:
        venue = "Hängmattan"
        address = "Karl Johansgatan 16"
    elif "stora" in venue_text:
        venue = "Musikens Hus, Stora Scen"
        address = "Djurgårdsgatan 13"
    elif re.search(r"Karl\s*Johans?gatan\s*16", text, re.I):
        venue = "Hängmattan"
        address = "Karl Johansgatan 16"

    doors = re.search(r"Vi\s+öppnar\s+kl\.?\s*([0-2]?\d[:.]\d{2})", text, re.I)
    desc_parts = []
    if doors:
        desc_parts.append(f"Öppnar {doors.group(1).replace('.', ':')}.")
    if time_note:
        desc_parts.append(time_note)
    start = local_dt(d, start_t)
    if venue == "Hängmattan":
        source_name = "Hängmattan"
    elif "Hängmattan" in venue:
        source_name = "Musikens Hus & Hängmattan"
    else:
        source_name = "Musikens Hus"
    return Event(
        title_from_soup(soup),
        start,
        sensible_end(start, end_t),
        venue,
        address,
        "Göteborg",
        source_name,
        url,
        " ".join(desc_parts),
    )


def _mh_listing_card(anchor) -> Any | None:
    """Return the smallest ancestor that looks like one Musikens Hus event card."""
    for parent in anchor.parents:
        name = getattr(parent, "name", None)
        if name in ("body", "html", None):
            break
        text = clean_text(parent.get_text(" ", strip=True))
        if not (25 <= len(text) <= 3500):
            continue
        heading = parent.find(["h1", "h2", "h3"])
        if heading and parse_swedish_date(text):
            return parent
    return anchor.parent


def _mh_listing_time(text: str) -> tuple[time, time | None, str]:
    patterns = [
        r"På\s+scen(?:\s+ca)?\s+(?:kl\.?\s*)?([0-2]?\d[:.]\d{2})",
        r"Start(?:ar|tid)?\s*(?:kl\.?\s*)?([0-2]?\d[:.]\d{2})",
        r"Från\s+([0-2]?\d[:.]\d{2})",
        r"Öppet\s*(?:kl\.?\s*)?([0-2]?\d[:.]\d{2})(?:\s*[-–]\s*([0-2]?\d[:.]\d{2}))?",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if not m:
            continue
        st = parse_hhmm(m.group(1))
        et = parse_hhmm(m.group(2)) if m.lastindex and m.lastindex >= 2 and m.group(2) else None
        if st:
            return st, et, ""
    return time(19, 0), None, "Starttid kunde inte utläsas ur kalenderlistan; 19:00 används som reservtid."


def parse_musikens_hus_listing(html: str, base_url: str, today: date) -> list[Event]:
    soup = BeautifulSoup(html, "html.parser")
    events: list[Event] = []
    seen_urls: set[str] = set()

    for a in soup.find_all("a", href=True):
        label = clean_text(a.get_text(" ", strip=True)).lower()
        href = urljoin(base_url, a.get("href", ""))
        p = urlparse(href)
        if label != "läs mer":
            continue
        if p.netloc not in ("www.musikenshus.se", "musikenshus.se"):
            continue
        if p.path in ("/", "/kalender/") or href in seen_urls:
            continue

        card = _mh_listing_card(a)
        if card is None:
            continue
        text = clean_text(card.get_text(" ", strip=True))
        d = parse_swedish_date(text, today)
        if not d:
            # Some themes keep the day/month just outside the clickable card.
            prev = []
            node = card
            for _ in range(7):
                node = node.find_previous() if node else None
                if node is None:
                    break
                t = clean_text(node.get_text(" ", strip=True)) if hasattr(node, "get_text") else ""
                if t:
                    prev.append(t)
                d = parse_swedish_date(" ".join(reversed(prev)), today)
                if d:
                    break
        if not d:
            continue

        heading = card.find(["h1", "h2", "h3"])
        title = clean_text(heading.get_text(" ", strip=True)) if heading else ""
        if not title or title.lower() in {"kalender", "musikens hus", "hängmattan"}:
            # The event title is normally the closest heading before the Läs mer link.
            prev_heading = a.find_previous(["h1", "h2", "h3"])
            title = clean_text(prev_heading.get_text(" ", strip=True)) if prev_heading else "Evenemang"

        lower = text.lower()
        if "hängmattan" in lower and ("stora scen" in lower or "stora scenen" in lower):
            venue = "Musikens Hus & Hängmattan"
            address = "Djurgårdsgatan 13 / Karl Johansgatan 16"
            source_name = "Musikens Hus & Hängmattan"
        elif "hängmattan" in lower:
            venue = "Hängmattan"
            address = "Karl Johansgatan 16"
            source_name = "Hängmattan"
        elif "stora scen" in lower or "stora scenen" in lower:
            venue = "Musikens Hus, Stora Scen"
            address = "Djurgårdsgatan 13"
            source_name = "Musikens Hus"
        else:
            venue = "Musikens Hus"
            address = "Djurgårdsgatan 13"
            source_name = "Musikens Hus"

        start_t, end_t, note = _mh_listing_time(text)
        start = local_dt(d, start_t)
        events.append(Event(
            title=title,
            start=start,
            end=sensible_end(start, end_t),
            venue=venue,
            address=address,
            city="Göteborg",
            source=source_name,
            url=href,
            description=note,
        ))
        seen_urls.add(href)

    return deduplicate(events)


def scrape_musikens_hus(session: requests.Session, today: date) -> list[Event]:
    calendar_url = SOURCES["Musikens Hus & Hängmattan"]
    index_html = fetch_musikens_hus_index(session, calendar_url)
    events = parse_musikens_hus_listing(index_html, calendar_url, today)
    if not events:
        raise SourceError("Musikens Hus/Hängmattan kunde hämtas men kalenderlistan gav inga tolkbara evenemang.")
    return events

def parse_billetto_page(html: str, url: str, today: date | None = None) -> Event | None:
    soup = BeautifulSoup(html, "html.parser")
    structured = parse_jsonld_event(soup, "Utopia Jazz", url)
    if structured:
        hay = " ".join([structured.venue, structured.address, structured.description]).lower()
        if "utopia" not in hay:
            return None
        structured.venue = "Utopia Jazz"
        structured.city = structured.city or "Göteborg"
        structured.address = structured.address or "Karl Johansgatan 6"
        return structured
    text = soup_text(soup)
    if "utopia jazz" not in text.lower():
        return None
    # Billetto commonly renders: Datum 2 apr 2026 20:00 - 23:00
    m = re.search(rf"Datum\s+(\d{{1,2}})\s+({MONTH_RE})\s+(\d{{4}})\s+([0-2]?\d[:.]\d{{2}})\s*-\s*(?:(\d{{1,2}})\s+({MONTH_RE})\s+(\d{{4}})\s+)?([0-2]?\d[:.]\d{{2}})", text, re.I)
    if not m:
        return None
    d = date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)))
    start_t = parse_hhmm(m.group(4))
    if not start_t:
        return None
    start = local_dt(d, start_t)
    if m.group(5):
        end_d = date(int(m.group(7)), MONTHS[m.group(6).lower()], int(m.group(5)))
    else:
        end_d = d
    end_t = parse_hhmm(m.group(8))
    end = datetime.combine(end_d, end_t) if end_t else start + timedelta(hours=3)
    if end <= start:
        end += timedelta(days=1)
    return Event(title_from_soup(soup), start, end, "Utopia Jazz", "Karl Johansgatan 6", "Göteborg", "Utopia Jazz", url)


def scrape_utopia(session: requests.Session, today: date) -> list[Event]:
    pages = [SOURCES["Utopia Jazz"], "https://utopiajazz.com/"]
    links: list[str] = []
    for page in pages:
        try:
            r = fetch(session, page)
            for link in extract_billetto_links(r.text, page):
                if link not in links:
                    links.append(link)
        except Exception as exc:
            logging.debug("Utopia index failed %s: %s", page, exc)
    if not links:
        raise SourceError("Billetto-sidan gav inga direkta eventlänkar. Den laddar sannolikt programmet dynamiskt.")
    events: list[Event] = []
    for link in links[:160]:
        try:
            ev = parse_billetto_page(fetch(session, link).text, link, today)
            if ev:
                events.append(ev)
        except Exception as exc:
            logging.debug("Utopia event failed %s: %s", link, exc)
    return events


def normalize_title(title: str) -> str:
    t = unicodedata.normalize("NFKD", title.lower())
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def deduplicate(events: list[Event]) -> list[Event]:
    result: list[Event] = []
    for ev in sorted(events, key=lambda x: (x.start, x.title)):
        duplicate = False
        a = normalize_title(ev.title)
        for existing in result:
            if ev.city.lower() != existing.city.lower():
                continue
            if abs((ev.start - existing.start).total_seconds()) > 90 * 60:
                continue
            b = normalize_title(existing.title)
            if a == b or SequenceMatcher(None, a, b).ratio() >= 0.92:
                duplicate = True
                # Prefer the event with richer text and keep source attribution.
                if len(ev.description) > len(existing.description):
                    existing.description = ev.description
                if ev.source not in existing.source.split(" + "):
                    existing.source += " + " + ev.source
                break
        if not duplicate:
            result.append(ev)
    return result


def filter_window(events: list[Event], today: date, past_days: int = 1, future_days: int = 370) -> list[Event]:
    lower = datetime.combine(today - timedelta(days=past_days), time.min)
    upper = datetime.combine(today + timedelta(days=future_days), time.max)
    return [e for e in events if lower <= e.start <= upper]


def ics_escape(value: str) -> str:
    return (value or "").replace("\\", "\\\\").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")


def fold_ics_line(line: str, limit: int = 73) -> list[str]:
    # Fold on Unicode characters, conservative enough to remain below 75 octets for normal Swedish text.
    if len(line) <= limit:
        return [line]
    out = [line[:limit]]
    rest = line[limit:]
    while rest:
        out.append(" " + rest[:limit-1])
        rest = rest[limit-1:]
    return out


def calendar_text(events: list[Event], name: str) -> str:
    now = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Jazzkalender Prototype//SV//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(name)}",
        f"X-WR-TIMEZONE:{TZID}",
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
        "X-PUBLISHED-TTL:PT12H",
        "BEGIN:VTIMEZONE",
        f"TZID:{TZID}",
        f"X-LIC-LOCATION:{TZID}",
        "BEGIN:DAYLIGHT",
        "TZOFFSETFROM:+0100",
        "TZOFFSETTO:+0200",
        "TZNAME:CEST",
        "DTSTART:19700329T020000",
        "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU",
        "END:DAYLIGHT",
        "BEGIN:STANDARD",
        "TZOFFSETFROM:+0200",
        "TZOFFSETTO:+0100",
        "TZNAME:CET",
        "DTSTART:19701025T030000",
        "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU",
        "END:STANDARD",
        "END:VTIMEZONE",
    ]
    for e in sorted(events, key=lambda x: (x.start, x.title)):
        location = ", ".join(x for x in [e.venue, e.address, e.city] if x)
        desc_parts = [e.description.strip()] if e.description.strip() else []
        desc_parts.append(f"Källa: {e.source}")
        if e.url:
            desc_parts.append(e.url)
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{e.uid}",
            f"DTSTAMP:{now}",
            f"DTSTART;TZID={TZID}:{e.start.strftime('%Y%m%dT%H%M%S')}",
            f"DTEND;TZID={TZID}:{e.end.strftime('%Y%m%dT%H%M%S')}",
            f"SUMMARY:{ics_escape(e.title)}",
            f"LOCATION:{ics_escape(location)}",
            f"DESCRIPTION:{ics_escape(chr(10).join(desc_parts))}",
            f"URL:{ics_escape(e.url)}" if e.url else "URL:",
            f"CATEGORIES:{ics_escape(e.source)}",
            "STATUS:CONFIRMED",
            "END:VEVENT",
        ])
    lines.append("END:VCALENDAR")
    folded: list[str] = []
    for line in lines:
        folded.extend(fold_ics_line(line))
    return "\r\n".join(folded) + "\r\n"


FALLBACK_EVENTS_URL = os.environ.get(
    "JAZZ_FALLBACK_URL",
    "https://hemmikablues.github.io/Blueskalender/events.json",
)

MH_CACHE_URL = os.environ.get(
    "JAZZ_MH_CACHE_URL",
    "https://hemmikablues.github.io/Blueskalender/cache/musikens_hus.json",
)

SOURCE_ALIASES = {
    "Fasching": {"Fasching"},
    "Nefertiti": {"Nefertiti"},
    "Playhouse": {"Playhouse"},
    "Skeppet GBG": {"Skeppet GBG"},
    "Unity Jazz": {"Unity Jazz"},
    "Musikens Hus & Hängmattan": {"Musikens Hus", "Hängmattan", "Musikens Hus & Hängmattan"},
    "Utopia Jazz": {"Utopia Jazz"},
}


def event_from_dict(data: dict[str, Any]) -> Event | None:
    try:
        start = datetime.fromisoformat(str(data["start"]))
        end = datetime.fromisoformat(str(data["end"]))
        return Event(
            title=str(data.get("title", "Evenemang")), start=start.replace(tzinfo=None), end=end.replace(tzinfo=None),
            venue=str(data.get("venue", "")), address=str(data.get("address", "")), city=str(data.get("city", "")),
            source=str(data.get("source", "")), url=str(data.get("url", "")), description=str(data.get("description", "")),
            category=str(data.get("category", "")),
        )
    except Exception:
        return None


def load_previous_events(session: requests.Session, today: date) -> list[Event]:
    if not FALLBACK_EVENTS_URL:
        return []
    try:
        response = fetch(session, FALLBACK_EVENTS_URL, timeout=15)
        raw = response.json()
        if not isinstance(raw, list):
            return []
        parsed = [event_from_dict(item) for item in raw if isinstance(item, dict)]
        return filter_window([e for e in parsed if e is not None], today)
    except Exception as exc:
        logging.info("Kunde inte läsa reservdata från föregående publicering: %s", exc)
        return []


def load_mh_source_cache(session: requests.Session, today: date) -> list[Event]:
    if not MH_CACHE_URL:
        return []
    try:
        response = fetch(session, MH_CACHE_URL, timeout=15)
        raw = response.json()
        if not isinstance(raw, list):
            return []
        parsed = [event_from_dict(item) for item in raw if isinstance(item, dict)]
        events = filter_window([e for e in parsed if e is not None], today)
        return previous_for_source(events, "Musikens Hus & Hängmattan")
    except Exception as exc:
        logging.info("Ingen separat Musikens Hus-cache kunde läsas ännu: %s", exc)
        return []


def previous_for_source(previous: list[Event], source_name: str) -> list[Event]:
    aliases = SOURCE_ALIASES.get(source_name, {source_name})
    result = []
    for event in previous:
        event_sources = {part.strip() for part in event.source.split(" + ") if part.strip()}
        if event_sources & aliases:
            result.append(event)
    return result


WEB_CSS = ':root {\n  --bg: #f6f3ec;\n  --surface: #fffdf8;\n  --ink: #17202a;\n  --muted: #66717d;\n  --line: #ded8cc;\n  --accent: #173d59;\n  --accent-2: #9a5a2d;\n  --warn: #8a4b08;\n  --warn-bg: #fff3d9;\n  --ok: #2e6846;\n  --radius: 18px;\n}\n* { box-sizing: border-box; }\nhtml { scroll-behavior: smooth; }\nbody { margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--ink); background: var(--bg); line-height: 1.5; }\na { color: inherit; }\nbutton, input, select { font: inherit; }\nbutton { cursor: pointer; }\n.hero { background: linear-gradient(135deg, #112c40 0%, #173d59 56%, #315d72 100%); color: white; padding: 42px 20px 34px; }\n.hero-inner, .page { max-width: 1120px; margin: 0 auto; }\n.eyebrow { margin: 0 0 6px; font-size: .78rem; letter-spacing: .14em; text-transform: uppercase; opacity: .78; }\nh1 { margin: 0; font-size: clamp(2.1rem, 5vw, 4rem); line-height: 1; letter-spacing: -.045em; }\n.hero-copy { max-width: 760px; margin: 16px 0 0; font-size: 1.05rem; opacity: .9; }\n.hero-actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 24px; }\n.copy-btn { border: 1px solid rgba(255,255,255,.42); background: rgba(255,255,255,.09); color: white; border-radius: 999px; padding: 9px 14px; }\n.copy-btn:hover { background: rgba(255,255,255,.17); }\n.page { padding: 28px 20px 64px; }\n.notice { display: none; margin-bottom: 18px; padding: 12px 14px; border: 1px solid #e5c482; background: var(--warn-bg); color: #5d390d; border-radius: 12px; }\n.notice.show { display: block; }\n.toolbar { position: sticky; top: 0; z-index: 10; background: rgba(246,243,236,.94); backdrop-filter: blur(12px); padding: 10px 0 14px; }\n.toolbar-row { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }\n.segmented { display: inline-flex; gap: 4px; padding: 4px; background: #e9e4da; border-radius: 999px; }\n.segmented button { border: 0; background: transparent; color: #34404b; border-radius: 999px; padding: 8px 13px; }\n.segmented button.active { background: var(--surface); color: var(--accent); box-shadow: 0 1px 5px rgba(0,0,0,.09); font-weight: 650; }\n.search { flex: 1 1 220px; min-width: 190px; border: 1px solid var(--line); background: var(--surface); border-radius: 999px; padding: 10px 14px; outline: none; }\n.search:focus { border-color: #7c9bad; box-shadow: 0 0 0 3px rgba(23,61,89,.1); }\n.source-select { border: 1px solid var(--line); background: var(--surface); color: var(--ink); border-radius: 999px; padding: 10px 36px 10px 13px; }\n.summary { display: flex; align-items: baseline; justify-content: space-between; gap: 16px; margin: 26px 0 8px; }\n.summary h2 { margin: 0; font-size: 1.35rem; letter-spacing: -.02em; }\n.summary p { margin: 0; color: var(--muted); font-size: .92rem; }\n.month-heading { margin: 36px 0 14px; font-size: 1.6rem; letter-spacing: -.025em; text-transform: capitalize; }\n.date-group { margin-top: 22px; }\n.date-heading { display: flex; align-items: baseline; gap: 9px; border-bottom: 1px solid var(--line); padding-bottom: 8px; margin-bottom: 10px; }\n.date-heading strong { font-size: 1.06rem; text-transform: capitalize; }\n.date-heading span { color: var(--muted); font-size: .9rem; }\n.event { display: grid; grid-template-columns: 86px minmax(0,1fr) auto; gap: 16px; align-items: start; background: var(--surface); border: 1px solid #e7e1d6; border-radius: var(--radius); padding: 17px 18px; margin: 10px 0; box-shadow: 0 3px 14px rgba(23,32,42,.035); }\n.event-time { font-variant-numeric: tabular-nums; color: var(--accent); font-weight: 750; font-size: 1.04rem; }\n.event-title { margin: 0; font-size: 1.11rem; letter-spacing: -.015em; }\n.event-meta { margin-top: 4px; color: var(--muted); font-size: .92rem; }\n.event-source { display: inline-block; margin-top: 8px; color: var(--accent-2); font-size: .79rem; font-weight: 700; letter-spacing: .02em; }\n.event-link { align-self: center; text-decoration: none; white-space: nowrap; color: var(--accent); border: 1px solid #b9c9d3; border-radius: 999px; padding: 7px 10px; font-size: .88rem; }\n.event-link:hover { background: #edf3f6; }\n.empty { display: none; text-align: center; padding: 58px 20px; color: var(--muted); }\n.empty.show { display: block; }\n.load-more-wrap { text-align: center; margin: 28px 0; }\n.load-more { border: 1px solid #a7b8c3; background: var(--surface); color: var(--accent); border-radius: 999px; padding: 10px 18px; font-weight: 650; }\n.status-panel { margin-top: 48px; border-top: 1px solid var(--line); padding-top: 22px; }\n.status-panel details { background: var(--surface); border: 1px solid var(--line); border-radius: 14px; padding: 0 16px; }\n.status-panel summary { cursor: pointer; padding: 14px 0; font-weight: 700; }\n.status-list { list-style: none; margin: 0; padding: 0 0 14px; }\n.status-list li { display: grid; grid-template-columns: minmax(180px,1fr) auto minmax(200px,2fr); gap: 14px; padding: 9px 0; border-top: 1px solid #eee8dc; font-size: .9rem; }\n.status-ok { color: var(--ok); font-weight: 700; }\n.status-warn { color: var(--warn); font-weight: 700; }\n.footer { color: var(--muted); font-size: .85rem; margin-top: 24px; text-align: center; }\n.toast { position: fixed; right: 18px; bottom: 18px; max-width: min(560px, calc(100vw - 36px)); background: #17202a; color: white; border-radius: 10px; padding: 10px 14px; opacity: 0; transform: translateY(8px); pointer-events: none; transition: .18s ease; }\n.toast.show { opacity: 1; transform: translateY(0); }\n@media (max-width: 700px) {\n  .hero { padding-top: 30px; }\n  .event { grid-template-columns: 68px minmax(0,1fr); gap: 10px; padding: 14px; }\n  .event-link { grid-column: 2; justify-self: start; }\n  .status-list li { grid-template-columns: 1fr auto; }\n  .status-list li span:last-child { grid-column: 1 / -1; }\n}\n'
WEB_JS = '(() => {\n  const state = { events: [], statuses: [], city: \'Alla\', source: \'Alla\', query: \'\', limit: 50 };\n  const el = id => document.getElementById(id);\n  const list = el(\'events\');\n  const empty = el(\'empty\');\n  const sourceSelect = el(\'source-select\');\n  const countLabel = el(\'event-count\');\n  const notice = el(\'notice\');\n  const statusList = el(\'status-list\');\n  const loadMore = el(\'load-more\');\n  const toast = el(\'toast\');\n\n  const normCity = c => {\n    const x = (c || \'\').toLowerCase();\n    if ([\'göteborg\', \'goteborg\', \'gothenburg\'].includes(x)) return \'Göteborg\';\n    if (x === \'stockholm\') return \'Stockholm\';\n    return c || \'Okänd\';\n  };\n  const dt = value => new Date(value);\n  const dateKey = value => {\n    const d = dt(value);\n    return [d.getFullYear(), String(d.getMonth() + 1).padStart(2, \'0\'), String(d.getDate()).padStart(2, \'0\')].join(\'-\');\n  };\n  const dateLabel = value => new Intl.DateTimeFormat(\'sv-SE\', { weekday: \'long\', day: \'numeric\', month: \'long\' }).format(dt(value));\n  const timeLabel = value => new Intl.DateTimeFormat(\'sv-SE\', { hour: \'2-digit\', minute: \'2-digit\' }).format(dt(value));\n  const monthYear = value => new Intl.DateTimeFormat(\'sv-SE\', { month: \'long\', year: \'numeric\' }).format(dt(value));\n  const esc = s => String(s ?? \'\').replace(/[&<>\'"]/g, ch => ({ \'&\':\'&amp;\', \'<\':\'&lt;\', \'>\':\'&gt;\', "\'":\'&#39;\', \'"\':\'&quot;\' }[ch]));\n\n  function filteredEvents() {\n    const q = state.query.trim().toLocaleLowerCase(\'sv-SE\');\n    return state.events.filter(e => {\n      if (state.city !== \'Alla\' && normCity(e.city) !== state.city) return false;\n      if (state.source !== \'Alla\' && e.source !== state.source) return false;\n      if (q) {\n        const hay = [e.title, e.venue, e.city, e.source, e.description].join(\' \').toLocaleLowerCase(\'sv-SE\');\n        if (!hay.includes(q)) return false;\n      }\n      return true;\n    });\n  }\n\n  function render() {\n    const all = filteredEvents();\n    const visible = all.slice(0, state.limit);\n    countLabel.textContent = `${all.length} evenemang`;\n    empty.classList.toggle(\'show\', all.length === 0);\n    loadMore.hidden = all.length <= state.limit;\n\n    let html = \'\';\n    let currentDate = \'\';\n    let currentMonth = \'\';\n    for (const e of visible) {\n      const key = dateKey(e.start);\n      const month = monthYear(e.start);\n      if (month !== currentMonth) {\n        currentMonth = month;\n        html += `<h2 class="month-heading">${esc(month)}</h2>`;\n      }\n      if (key !== currentDate) {\n        currentDate = key;\n        html += `<section class="date-group"><div class="date-heading"><strong>${esc(dateLabel(e.start))}</strong><span>${esc(key)}</span></div></section>`;\n      }\n      const venue = [e.venue, normCity(e.city)].filter(Boolean).join(\', \');\n      const source = e.source || e.venue || \'\';\n      html += `<article class="event">\n        <div class="event-time">${esc(timeLabel(e.start))}</div>\n        <div>\n          <h3 class="event-title">${esc(e.title)}</h3>\n          <div class="event-meta">${esc(venue)}</div>\n          <span class="event-source">${esc(source)}</span>\n        </div>\n        ${e.url ? `<a class="event-link" href="${esc(e.url)}" target="_blank" rel="noopener">Mer info ↗</a>` : \'\'}\n      </article>`;\n    }\n    list.innerHTML = html;\n  }\n\n  function populateSources() {\n    const sources = [...new Set(state.events.map(e => e.source).filter(Boolean))].sort((a, b) => a.localeCompare(b, \'sv\'));\n    sourceSelect.innerHTML = \'<option value="Alla">Alla arrangörer</option>\' + sources.map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join(\'\');\n  }\n\n  function renderStatuses() {\n    const warnings = state.statuses.filter(s => (!s.ok || s.using_fallback) && s.source !== \'Utopia Jazz\');\n    if (warnings.length) {\n      const names = warnings.map(s => s.source).join(\', \');\n      notice.textContent = `Obs: ${names} har en varning i senaste hämtningen. Om reservdata finns visas senaste kända evenemang.`;\n      notice.classList.add(\'show\');\n    }\n    statusList.innerHTML = state.statuses.map(s => {\n      const cls = s.ok && !s.using_fallback ? \'status-ok\' : \'status-warn\';\n      const label = s.using_fallback ? \'Reservdata\' : (s.ok ? \'OK\' : \'Varning\');\n      const info = s.message || (s.latest ? `Senaste datum ${s.latest}` : \'\');\n      return `<li><strong>${esc(s.source)}</strong><span class="${cls}">${esc(label)} · ${Number(s.count || 0)}</span><span>${esc(info)}</span></li>`;\n    }).join(\'\');\n  }\n\n  document.querySelectorAll(\'[data-city]\').forEach(btn => btn.addEventListener(\'click\', () => {\n    document.querySelectorAll(\'[data-city]\').forEach(b => b.classList.remove(\'active\'));\n    btn.classList.add(\'active\');\n    state.city = btn.dataset.city;\n    state.limit = 50;\n    render();\n  }));\n  sourceSelect.addEventListener(\'change\', () => { state.source = sourceSelect.value; state.limit = 50; render(); });\n  el(\'search\').addEventListener(\'input\', e => { state.query = e.target.value; state.limit = 50; render(); });\n  loadMore.addEventListener(\'click\', () => { state.limit += 50; render(); });\n\n  document.querySelectorAll(\'[data-copy]\').forEach(btn => btn.addEventListener(\'click\', async () => {\n    const url = new URL(btn.dataset.copy, window.location.href).href;\n    try {\n      await navigator.clipboard.writeText(url);\n      toast.textContent = \'Kalenderlänken är kopierad\';\n    } catch {\n      toast.textContent = url;\n    }\n    toast.classList.add(\'show\');\n    setTimeout(() => toast.classList.remove(\'show\'), 2400);\n  }));\n\n  Promise.all([\n    fetch(\'events.json\', { cache: \'no-store\' }).then(r => { if (!r.ok) throw new Error(\'events.json\'); return r.json(); }),\n    fetch(\'status.json\', { cache: \'no-store\' }).then(r => { if (!r.ok) throw new Error(\'status.json\'); return r.json(); })\n  ]).then(([events, statuses]) => {\n    state.events = events.sort((a, b) => dt(a.start) - dt(b.start));\n    state.statuses = statuses;\n    populateSources();\n    renderStatuses();\n    render();\n  }).catch(err => {\n    notice.textContent = \'Kalenderdata kunde inte läsas just nu. Prova att ladda om sidan om en stund.\';\n    notice.classList.add(\'show\');\n    console.error(err);\n  });\n})();\n'
WEB_HTML = '<!doctype html>\n<html lang="sv">\n<head>\n  <meta charset="utf-8">\n  <meta name="viewport" content="width=device-width, initial-scale=1">\n  <meta name="description" content="Samlade kommande konserter och musikevenemang i Göteborg och Stockholm.">\n  <title>Musikkalender</title>\n  <link rel="stylesheet" href="style.css?v=__VERSION__">\n</head>\n<body>\n  <header class="hero">\n    <div class="hero-inner">\n      <p class="eyebrow">Göteborg · Stockholm</p>\n      <h1>Musikkalender</h1>\n      <p class="hero-copy">En samlad kalender med evenemang från Fasching, Nefertiti, Playhouse, Skeppet, Unity Jazz samt Musikens Hus och Hängmattan.</p>\n      <div class="hero-actions" aria-label="Kalenderprenumerationer">\n        <button class="copy-btn" data-copy="goteborg.ics">Kopiera Göteborgslänk</button>\n        <button class="copy-btn" data-copy="stockholm.ics">Kopiera Stockholmslänk</button>\n        <button class="copy-btn" data-copy="alla.ics">Kopiera länk till alla</button>\n      </div>\n    </div>\n  </header>\n\n  <main class="page">\n    <div id="notice" class="notice" role="status"></div>\n    <div class="toolbar">\n      <div class="toolbar-row">\n        <div class="segmented" aria-label="Filtrera på stad">\n          <button class="active" data-city="Alla">Alla</button>\n          <button data-city="Göteborg">Göteborg</button>\n          <button data-city="Stockholm">Stockholm</button>\n        </div>\n        <input id="search" class="search" type="search" placeholder="Sök artist, scen eller arrangör" aria-label="Sök evenemang">\n        <select id="source-select" class="source-select" aria-label="Filtrera på arrangör"><option>Alla arrangörer</option></select>\n      </div>\n    </div>\n\n    <div class="summary">\n      <h2>Kommande evenemang</h2>\n      <p id="event-count">Läser kalender…</p>\n    </div>\n    <div id="events"></div>\n    <div id="empty" class="empty">Inga evenemang matchar det valda filtret.</div>\n    <div class="load-more-wrap"><button id="load-more" class="load-more" hidden>Visa fler</button></div>\n\n    <section class="status-panel">\n      <details>\n        <summary>Källstatus och teknisk information</summary>\n        <ul id="status-list" class="status-list"></ul>\n      </details>\n      <p class="footer">Kalendern uppdateras automatiskt. Senast genererad __UPDATED__. Version __VERSION__.</p>\n    </section>\n  </main>\n  <div id="toast" class="toast" role="status" aria-live="polite"></div>\n  <script src="app.js?v=__VERSION__" defer></script>\n</body>\n</html>\n'


def web_index(updated: str) -> str:
    return WEB_HTML.replace("__VERSION__", CALENDAR_VERSION).replace("__UPDATED__", html_lib.escape(updated))


def write_outputs(events: list[Event], statuses: list[SourceStatus], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    goteborg = [e for e in events if e.city.lower() in ("göteborg", "goteborg", "gothenburg")]
    stockholm = [e for e in events if e.city.lower() == "stockholm"]
    (output_dir / "alla.ics").write_text(calendar_text(events, "Jazzkalender, alla"), encoding="utf-8", newline="")
    (output_dir / "goteborg.ics").write_text(calendar_text(goteborg, "Jazzkalender, Göteborg"), encoding="utf-8", newline="")
    (output_dir / "stockholm.ics").write_text(calendar_text(stockholm, "Jazzkalender, Stockholm"), encoding="utf-8", newline="")
    payload = [asdict(e) | {"start": e.start.isoformat(), "end": e.end.isoformat()} for e in sorted(events, key=lambda x: (x.start, x.title))]
    (output_dir / "events.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "status.json").write_text(json.dumps([asdict(s) for s in statuses], ensure_ascii=False, indent=2), encoding="utf-8")
    mh_events = previous_for_source(events, "Musikens Hus & Hängmattan")
    if mh_events:
        cache_dir = output_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        mh_payload = [asdict(e) | {"start": e.start.isoformat(), "end": e.end.isoformat()} for e in sorted(mh_events, key=lambda x: (x.start, x.title))]
        (cache_dir / "musikens_hus.json").write_text(json.dumps(mh_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    updated = datetime.now().strftime("%Y-%m-%d %H:%M")
    (output_dir / "index.html").write_text(web_index(updated), encoding="utf-8")
    (output_dir / "style.css").write_text(WEB_CSS, encoding="utf-8")
    (output_dir / "app.js").write_text(WEB_JS, encoding="utf-8")
    (output_dir / ".nojekyll").write_text("", encoding="utf-8")


def run(today: date, output_dir: Path) -> tuple[list[Event], list[SourceStatus]]:
    session = make_session()
    previous = load_previous_events(session, today)
    mh_source_cache = load_mh_source_cache(session, today)
    if previous:
        logging.info("Läste %d evenemang från föregående publicering som reservdata.", len(previous))
    if mh_source_cache:
        logging.info("Läste %d Musikens Hus/Hängmattan-poster från separat källcache.", len(mh_source_cache))
    scrapers = [
        ("Fasching", scrape_fasching), ("Nefertiti", scrape_nefertiti), ("Playhouse", scrape_playhouse),
        ("Skeppet GBG", scrape_skeppet), ("Unity Jazz", scrape_unity),
        ("Musikens Hus & Hängmattan", scrape_musikens_hus), ("Utopia Jazz", scrape_utopia),
    ]
    all_events: list[Event] = []
    statuses: list[SourceStatus] = []
    for name, scraper in scrapers:
        prior = filter_window(previous_for_source(previous, name), today)
        if name == "Musikens Hus & Hängmattan" and mh_source_cache:
            # Den separata cachen är avsiktligt starkare än den allmänna events.json-cachen.
            # Därmed kan en tidigare misslyckad publicering inte radera källans reservdata.
            prior = filter_window(mh_source_cache, today)
        try:
            events = filter_window(scraper(session, today), today)
            if events:
                all_events.extend(events)
                latest = max(e.start.date() for e in events)
                status_message = ""
                statuses.append(SourceStatus(name, True, len(events), latest.isoformat(), status_message, False))
                logging.info("%s: %d evenemang, senaste %s", name, len(events), latest)
            elif prior:
                all_events.extend(prior)
                latest = max(e.start.date() for e in prior)
                msg = f"Senaste hämtningen gav 0 evenemang. Visar {len(prior)} senast kända poster från föregående publicering."
                statuses.append(SourceStatus(name, False, len(prior), latest.isoformat(), msg, True))
                logging.warning("%s: använder reservdata (%d evenemang)", name, len(prior))
            else:
                statuses.append(SourceStatus(name, False, 0, "", "Inga framtida evenemang hittades. Kontrollera källan.", False))
        except Exception as exc:
            if prior:
                all_events.extend(prior)
                latest = max(e.start.date() for e in prior)
                msg = f"Källan svarade inte: {exc}. Visar {len(prior)} senast kända poster från föregående publicering."
                statuses.append(SourceStatus(name, False, len(prior), latest.isoformat(), msg, True))
                logging.warning("%s misslyckades, använder reservdata: %s", name, exc)
            else:
                statuses.append(SourceStatus(name, False, 0, "", str(exc), False))
                logging.warning("%s misslyckades: %s", name, exc)
    all_events = deduplicate(filter_window(all_events, today))
    write_outputs(all_events, statuses, output_dir)
    return all_events, statuses

def main() -> int:
    ap = argparse.ArgumentParser(description="Samlar sju eventkalendrar till prenumererbara ICS-filer.")
    ap.add_argument("--output-dir", default="public", help="Katalog för genererade filer")
    ap.add_argument("--date", help="Överstyr dagens datum, YYYY-MM-DD, praktiskt för test")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s: %(message)s")
    today = date.fromisoformat(args.date) if args.date else date.today()
    events, statuses = run(today, Path(args.output_dir))
    ok = sum(1 for s in statuses if s.ok)
    print(f"Skrev {len(events)} evenemang. {ok}/{len(statuses)} källor lyckades.")
    # Prototype should still publish partial calendars when one source is temporarily unavailable.
    return 0 if ok >= 3 else 2


if __name__ == "__main__":
    sys.exit(main())
