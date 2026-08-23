#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import html as html_lib
import json
import logging
import re
import sys
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
    message: str = ""


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


def scrape_fasching(session: requests.Session, today: date) -> list[Event]:
    url = SOURCES["Fasching"]
    soup = BeautifulSoup(fetch(session, url).text, "html.parser")

    def pred(href: str, a) -> bool:
        p = urlparse(href)
        if p.netloc not in ("www.fasching.se", "fasching.se"):
            return False
        if re.search(r"-20\d{2}-\d{2}-\d{2}/?$", p.path):
            return True
        ancestor = a
        for _ in range(4):
            ancestor = ancestor.parent
            if ancestor is None:
                break
            txt = clean_text(ancestor.get_text(" ", strip=True)).lower()
            if re.search(rf"\b{WEEKDAY_RE}\s+\d{{1,2}}\s+{MONTH_RE}\b", txt, re.I):
                return p.path.count("/") <= 2 and p.path not in ("/", "/kalendarium/")
        return False

    links = event_links(soup, url, pred)
    events = []
    for link in links[:120]:
        try:
            ev = parse_fasching_page(fetch(session, link).text, link, today)
            if ev:
                events.append(ev)
        except Exception as exc:
            logging.debug("Fasching event failed %s: %s", link, exc)
    return events


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


def scrape_nefertiti(session: requests.Session, today: date) -> list[Event]:
    url = SOURCES["Nefertiti"]
    soup = BeautifulSoup(fetch(session, url).text, "html.parser")
    links = event_links(soup, url, lambda href, a: "/nefertiti_event/" in urlparse(href).path)
    events = []
    for link in links[:160]:
        try:
            ev = parse_nefertiti_page(fetch(session, link).text, link, today)
            if ev:
                events.append(ev)
        except Exception as exc:
            logging.debug("Nefertiti event failed %s: %s", link, exc)
    return events


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


def write_outputs(events: list[Event], statuses: list[SourceStatus], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    goteborg = [e for e in events if e.city.lower() in ("göteborg", "goteborg", "gothenburg")]
    stockholm = [e for e in events if e.city.lower() == "stockholm"]
    (output_dir / "alla.ics").write_text(calendar_text(events, "Jazzkalender, alla"), encoding="utf-8", newline="")
    (output_dir / "goteborg.ics").write_text(calendar_text(goteborg, "Jazzkalender, Göteborg"), encoding="utf-8", newline="")
    (output_dir / "stockholm.ics").write_text(calendar_text(stockholm, "Jazzkalender, Stockholm"), encoding="utf-8", newline="")
    (output_dir / "events.json").write_text(json.dumps([asdict(e) | {"start": e.start.isoformat(), "end": e.end.isoformat()} for e in events], ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "status.json").write_text(json.dumps([asdict(s) for s in statuses], ensure_ascii=False, indent=2), encoding="utf-8")
    index = """<!doctype html><meta charset='utf-8'><title>Jazzkalender</title>
<h1>Jazzkalender</h1>
<ul><li><a href='alla.ics'>Alla</a></li><li><a href='goteborg.ics'>Göteborg</a></li><li><a href='stockholm.ics'>Stockholm</a></li></ul>
<p>Kalendrarna uppdateras automatiskt.</p>"""
    (output_dir / "index.html").write_text(index, encoding="utf-8")


def run(today: date, output_dir: Path) -> tuple[list[Event], list[SourceStatus]]:
    session = make_session()
    scrapers = [
        ("Fasching", scrape_fasching),
        ("Nefertiti", scrape_nefertiti),
        ("Playhouse", scrape_playhouse),
        ("Skeppet GBG", scrape_skeppet),
        ("Unity Jazz", scrape_unity),
        ("Utopia Jazz", scrape_utopia),
    ]
    all_events: list[Event] = []
    statuses: list[SourceStatus] = []
    for name, scraper in scrapers:
        try:
            events = scraper(session, today)
            events = filter_window(events, today)
            all_events.extend(events)
            statuses.append(SourceStatus(name, True, len(events), ""))
            logging.info("%s: %d evenemang", name, len(events))
        except Exception as exc:
            statuses.append(SourceStatus(name, False, 0, str(exc)))
            logging.warning("%s misslyckades: %s", name, exc)
    all_events = deduplicate(filter_window(all_events, today))
    write_outputs(all_events, statuses, output_dir)
    return all_events, statuses


def main() -> int:
    ap = argparse.ArgumentParser(description="Samlar sex eventkalendrar till prenumererbara ICS-filer.")
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
