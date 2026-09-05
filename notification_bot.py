#!/usr/bin/env python3
"""Aggregate apartment-alert emails and forward interesting listings to Telegram."""

from __future__ import annotations

import argparse
import hashlib
import html
import imaplib
import json
import math
import os
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email import policy
from email.header import decode_header
from email.message import EmailMessage, Message
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
DEFAULT_STATE_FILE = ROOT / ".notification-bot.sqlite3"
USER_AGENT = "TU-Wien-apartment-alert-bot/1.0 (+personal, low-frequency search)"
WILLHABEN_BASE = "https://www.willhaben.at/iad/immobilien/mietwohnungen/wien/"
WILLHABEN_DISTRICTS = (
    "wien-1020-leopoldstadt",
    "wien-1040-wieden",
    "wien-1050-margareten",
    "wien-1100-favoriten",
)
WG_GESUCHT_SEARCH_URL = (
    "https://www.wg-gesucht.de/wg-zimmer-in-Wien.163.0.1.0.html"
    "?offer_filter=1&city_id=163&noDeact=1&categories%5B%5D=0"
    "&rent_types%5B%5D=0&max_rent={max_rent}"
)
WG_GESUCHT_APARTMENT_URL = (
    "https://www.wg-gesucht.de/wohnungen-in-Wien.163.2.1.0.html"
    "?offer_filter=1&city_id=163&noDeact=1&categories%5B%5D=2"
    "&max_rent={max_rent}"
)
OEH_SEARCH_URL = "https://schwarzesbrett.oeh.ac.at/wohnen/"


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = next((value for key, value in attrs if key.lower() == "href"), None)
        if href:
            self.parts.append(href)

    def text(self) -> str:
        return " ".join(self.parts)


class NextDataExtractor(HTMLParser):
    """Extract Next.js' JSON payload without depending on third-party packages."""

    def __init__(self) -> None:
        super().__init__()
        self.capture = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag.lower() == "script" and attributes.get("id") == "__NEXT_DATA__":
            self.capture = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script":
            self.capture = False

    def handle_data(self, data: str) -> None:
        if self.capture:
            self.parts.append(data)

    def json_text(self) -> str:
        return "".join(self.parts)


@dataclass(frozen=True)
class Alert:
    uid: int
    fingerprint: str
    sender: str
    subject: str
    body: str
    prices: tuple[int, ...]
    postcode: str | None
    priority: str
    urls: tuple[str, ...]
    contract_months: int | None
    contract_term: str


@dataclass(frozen=True)
class Listing:
    uid: int
    fingerprint: str
    source: str
    listing_id: str
    title: str
    url: str
    price: int | None
    postcode: str | None
    address: str
    available: str
    size: str
    priority: str
    distance_km: float | None = None
    rooms: float | None = None
    search_profile: str = "Single room"
    contract_months: int | None = None
    contract_term: str = "not specified"


@dataclass(frozen=True)
class ContractTerm:
    months: int | None
    label: str


_CONTRACT_CACHE: dict[str, tuple[float, ContractTerm]] = {}


def load_env_file(path: Path) -> None:
    """Load a simple KEY=VALUE file without replacing exported variables."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def env_list(name: str, default: str) -> tuple[str, ...]:
    return tuple(item.strip().lower() for item in os.getenv(name, default).split(",") if item.strip())


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def extract_contract_term(text: str) -> ContractTerm:
    """Extract a fixed contract duration from German or English listing text."""
    normalized = re.sub(r"\s+", " ", text).strip().lower()
    unlimited_terms = ("unbefristet", "unlimited contract", "indefinite contract", "open-ended")
    if any(term in normalized for term in unlimited_terms):
        return ContractTerm(None, "unlimited")

    duration_patterns = (
        r"(?:befrist(?:et|ung)?|miet(?:dauer|zeit)|vertrags(?:dauer|laufzeit)|"
        r"contract(?: duration| term)?|lease(?: duration| term)?|nur für|for)"
        r"\s*(?::|auf)?\s*(\d+(?:[.,]\d+)?)\s*(monate?n?|months?|jahre?|years?)",
        r"(\d+(?:[.,]\d+)?)\s*(monate?n?|months?|jahre?|years?)"
        r"\s*(?:befristet|mietdauer|mietzeit|vertragsdauer|contract|lease)",
        r"(\d+(?:[.,]\d+)?)\s*[- ]\s*(month|year)(?:\s+contract|\s+lease)?",
    )
    for pattern in duration_patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if not match:
            continue
        value = float(match.group(1).replace(",", "."))
        unit = match.group(2).lower()
        months = int(round(value * 12)) if unit.startswith(("jahr", "year")) else int(round(value))
        return ContractTerm(months, f"{months} months")

    date_pattern = r"(\d{2}\.\d{2}\.\d{4})\s*(?:bis|to|until|–|—|-)\s*(\d{2}\.\d{2}\.\d{4})"
    date_match = re.search(date_pattern, normalized, flags=re.IGNORECASE)
    if date_match:
        try:
            start = datetime.strptime(date_match.group(1), "%d.%m.%Y")
            end = datetime.strptime(date_match.group(2), "%d.%m.%Y")
            if end > start:
                months = max(1, int(round((end - start).days / 30.4375)))
                return ContractTerm(months, f"{months} months (from dates)")
        except ValueError:
            pass
    return ContractTerm(None, "not specified")


def contract_is_allowed(term: ContractTerm) -> bool:
    if term.months is None:
        return False
    minimum = int(os.getenv("MIN_CONTRACT_MONTHS", "6"))
    maximum = int(os.getenv("MAX_CONTRACT_MONTHS", "24"))
    return minimum <= term.months <= maximum


def contract_sort_distance(months: int | None) -> int:
    ideal = int(os.getenv("IDEAL_CONTRACT_MONTHS", "12"))
    return abs(months - ideal) if months is not None else 999


def fetch_page(url: str, timeout: int = 20) -> str:
    request = Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "de,en;q=0.8"},
    )
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def clean_html(fragment: str) -> str:
    parser = TextExtractor()
    parser.feed(fragment)
    return re.sub(r"\s+", " ", html.unescape(parser.text())).strip()


def listing_fingerprint(
    source: str,
    listing_id: str,
    price: int | None,
    search_profile: str = "Single room",
) -> str:
    # A meaningful price change should produce a fresh alert; ordinary page edits
    # should not repeatedly notify the user about the same room.
    profile_suffix = "" if search_profile == "Single room" else f":{search_profile}"
    value = f"direct:{source}:{listing_id}:{price if price is not None else 'unknown'}{profile_suffix}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def distance_to_tu_km(coordinates: str) -> float | None:
    """Straight-line distance to Favoritenstraße 9–11 for coarse filtering."""
    try:
        latitude, longitude = (float(value.strip()) for value in coordinates.split(",", 1))
    except (ValueError, AttributeError):
        return None
    tu_latitude, tu_longitude = 48.1983, 16.3700
    latitude_delta = math.radians(latitude - tu_latitude)
    longitude_delta = math.radians(longitude - tu_longitude)
    haversine = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(math.radians(tu_latitude))
        * math.cos(math.radians(latitude))
        * math.sin(longitude_delta / 2) ** 2
    )
    return 6371.0 * 2 * math.asin(math.sqrt(haversine))


def classify_whole_apartment(price: int | None, rooms: float | None, text: str) -> str | None:
    if price is None or rooms is None:
        return None
    lowered = text.lower()
    if any(term in lowered for term in ("keine wg", "nicht wg geeignet", "nicht wg-geeignet")):
        return None
    explicitly_wg_suitable = bool(
        re.search(r"\bwg[ -]?(?:geeignet|tauglich)\b|\b[23]er[ -]?wg\b", lowered)
    )
    group_3_budget = int(os.getenv("GROUP_3_MAX_RENT_EUR", "1500"))
    group_2_budget = int(os.getenv("GROUP_2_MAX_RENT_EUR", "1600"))
    group_3_min_rooms = float(os.getenv("GROUP_3_MIN_ROOMS", "3"))
    group_2_min_rooms = float(os.getenv("GROUP_2_MIN_ROOMS", "2"))
    group_3_required = group_3_min_rooms if explicitly_wg_suitable else group_3_min_rooms + 1
    group_2_required = group_2_min_rooms if explicitly_wg_suitable else group_2_min_rooms + 1
    if rooms >= group_3_required and price <= group_3_budget:
        return "Whole apartment for 3"
    if rooms >= group_2_required and price <= group_2_budget:
        return "Whole apartment for 2"
    return None


def attribute_map(item: dict[str, object]) -> dict[str, str]:
    result: dict[str, str] = {}
    attributes = item.get("attributes", {})
    if not isinstance(attributes, dict):
        return result
    entries = attributes.get("attribute", [])
    if not isinstance(entries, list):
        return result
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        values = entry.get("values")
        if isinstance(name, str) and isinstance(values, list) and values:
            result[name] = str(values[0])
    return result


def parse_willhaben_page(page: str, max_rent: int, allowed_postcodes: set[str]) -> list[Listing]:
    parser = NextDataExtractor()
    parser.feed(page)
    if not parser.json_text():
        raise ValueError("Willhaben page did not contain __NEXT_DATA__")
    payload = json.loads(parser.json_text())
    items = (
        payload.get("props", {})
        .get("pageProps", {})
        .get("searchResult", {})
        .get("advertSummaryList", {})
        .get("advertSummary", [])
    )
    if not isinstance(items, list):
        raise ValueError("Willhaben search payload has an unexpected format")

    listings: list[Listing] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        status = item.get("advertStatus", {})
        if isinstance(status, dict) and status.get("id") not in {None, "active"}:
            continue
        attributes = attribute_map(item)
        postcode = attributes.get("POSTCODE")
        if postcode not in allowed_postcodes:
            continue
        raw_price = attributes.get("PRICE") or attributes.get("RENT/PER_MONTH_LETTINGS")
        try:
            price = int(round(float(raw_price))) if raw_price is not None else None
        except ValueError:
            price = None
        if price is not None and price < 200:
            continue
        raw_rooms = attributes.get("NUMBER_OF_ROOMS") or attributes.get("ROOMS", "").split("X", 1)[0]
        try:
            rooms = float(raw_rooms) if raw_rooms else None
        except ValueError:
            rooms = None
        property_type = attributes.get("PROPERTY_TYPE", "")
        is_room = property_type == "Zimmer/WG"
        if is_room:
            if price is not None and price > max_rent:
                continue
            search_profile = "Single room"
        else:
            search_profile = classify_whole_apartment(price, rooms, f"{item.get('description', '')} {attributes.get('BODY_DYN', '')}")
            if search_profile is None:
                if price is not None and price <= max_rent:
                    # Preserve the original low-cost studio search.
                    search_profile = "Low-cost whole apartment"
                else:
                    continue
        listing_id = str(item.get("id") or attributes.get("ADID") or "")
        seo_path = attributes.get("SEO_URL", "")
        if not listing_id or not seo_path:
            continue
        title = str(item.get("description") or attributes.get("HEADING") or "Willhaben accommodation")
        address = attributes.get("ADDRESS") or attributes.get("LOCATION") or f"{postcode} Wien"
        body = attributes.get("BODY_DYN", "")
        searchable = f"{title} {body}".lower()
        contract = extract_contract_term(searchable)
        if contract.label != "not specified" and not contract_is_allowed(contract):
            continue
        rejected_terms = (
            "reserviert",
            "wohnung ist vergeben",
            "keine anfragen mehr",
            "vormerkschein",
            "wohnticket",
            "gemeindewohnung",
            "genossenschaftswohnung",
            "sozialbau",
        )
        if any(term in searchable for term in rejected_terms):
            continue
        distance_km = distance_to_tu_km(attributes.get("COORDINATES", ""))
        max_distance_km = float(os.getenv("DIRECT_MAX_DISTANCE_KM", "4.0"))
        if distance_km is not None and distance_km > max_distance_km:
            continue
        size = attributes.get("ESTATE_SIZE/LIVING_AREA") or attributes.get("ESTATE_SIZE") or ""
        available = attributes.get("AVAILABLE_DATE") or attributes.get("PUBLISHED_String", "")[:10]
        listings.append(
            Listing(
                uid=0,
                fingerprint=listing_fingerprint("willhaben", listing_id, price, search_profile),
                source="Willhaben",
                listing_id=listing_id,
                title=title,
                url=urljoin("https://www.willhaben.at/iad/", seo_path),
                price=price,
                postcode=postcode,
                address=address,
                available=available,
                size=f"{size} m²" if size else "",
                priority=classify_priority(f"{title} {address} {body}", postcode),
                distance_km=distance_km,
                rooms=rooms,
                search_profile=search_profile,
                contract_months=contract.months,
                contract_term=contract.label,
            )
        )
    return listings


def parse_wg_gesucht_page(
    page: str,
    max_rent: int,
    allowed_postcodes: set[str],
    whole_apartments: bool = False,
) -> list[Listing]:
    starts = list(re.finditer(r'<div\s+id="liste-details-ad-(\d+)"', page, flags=re.IGNORECASE))
    district_postcodes = {"02": "1020", "04": "1040", "05": "1050", "10": "1100"}
    listings: list[Listing] = []
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(page)
        card = page[match.start():end]
        text = clean_html(card)
        district_match = re.search(r"\b(\d{2})\.\s*Bezirk\b", text)
        postcode = district_postcodes.get(district_match.group(1)) if district_match else None
        if postcode not in allowed_postcodes:
            continue
        href_match = re.search(r'href="([^"]+\.\d+\.html)"', card, flags=re.IGNORECASE)
        title_match = re.search(r'<h2[^>]*>[\s\S]*?<a[^>]*>([\s\S]*?)</a>', card, flags=re.IGNORECASE)
        price_match = re.search(r"\b([0-9][0-9.]*)\s*(?:€|&euro;)", card, flags=re.IGNORECASE)
        if not href_match or not title_match or not price_match:
            continue
        price = int(price_match.group(1).replace(".", ""))
        rooms_match = re.search(r"\b(\d+(?:[,.]\d+)?)\s*-?\s*Zimmer-Wohnung\b", text, flags=re.IGNORECASE)
        rooms = float(rooms_match.group(1).replace(",", ".")) if rooms_match else None
        if whole_apartments:
            title_preview = clean_html(title_match.group(1)).lower()
            if any(term in title_preview for term in ("mitbewohner", "wg-zimmer", "roommate")):
                continue
            search_profile = classify_whole_apartment(price, rooms, f"{title_preview} {text}")
            if search_profile is None:
                continue
        else:
            if price > max_rent:
                continue
            search_profile = "Single room"
        listing_id = match.group(1)
        title = clean_html(title_match.group(1))
        contract = extract_contract_term(text)
        if contract.label != "not specified" and not contract_is_allowed(contract):
            continue
        available_match = re.search(r"Verfügbar:\s*([0-9.]+)", text, flags=re.IGNORECASE)
        if not available_match:
            available_match = re.search(r"\b(\d{2}\.\d{2}\.\d{4})\b", text)
        if whole_apartments and available_match:
            try:
                available_date = datetime.strptime(available_match.group(1), "%d.%m.%Y")
                latest_move_in = datetime.strptime(
                    os.getenv("LATEST_MOVE_IN_DATE", "31.10.2026"), "%d.%m.%Y"
                )
                if available_date > latest_move_in:
                    continue
            except ValueError:
                pass
        size_match = re.search(r"\b(\d+(?:[,.]\d+)?)\s*m²\b", text)
        address_match = re.search(r"\d{2}\.\s*Bezirk\s+[^|]+\|\s*([^|]+?)(?:\s+\d+\s*€|\s+\d{2}\.\d{2}\.\d{4})", text)
        address = address_match.group(1).strip() if address_match else f"{postcode} Wien"
        far_location_terms = (
            "klederinger",
            "oberlaa",
            "unterlaa",
            "otto-probst",
            "wienerberg",
        )
        if postcode == "1100" and any(term in address.lower() for term in far_location_terms):
            continue
        listings.append(
            Listing(
                uid=0,
                fingerprint=listing_fingerprint("wg-gesucht", listing_id, price, search_profile),
                source="WG-Gesucht",
                listing_id=listing_id,
                title=title,
                url=urljoin("https://www.wg-gesucht.de/", html.unescape(href_match.group(1))),
                price=price,
                postcode=postcode,
                address=address,
                available=available_match.group(1) if available_match else "",
                size=f"{size_match.group(1)} m²" if size_match else "",
                priority=classify_priority(f"{title} {address}", postcode),
                rooms=rooms,
                search_profile=search_profile,
                contract_months=contract.months,
                contract_term=contract.label,
            )
        )
    return listings


def parse_oeh_page(page: str, max_rent: int, allowed_postcodes: set[str]) -> list[Listing]:
    listings: list[Listing] = []
    for card in re.findall(r"<li[^>]*>([\s\S]*?)</li>", page, flags=re.IGNORECASE):
        href_match = re.search(r'href="([^"]+)"', card, flags=re.IGNORECASE)
        title_match = re.search(r"<h3[^>]*>[\s\S]*?<a[^>]*>([\s\S]*?)</a>", card, flags=re.IGNORECASE)
        price_match = re.search(r"([0-9][0-9.,]*)\s*EUR", card, flags=re.IGNORECASE)
        postcode_match = re.search(r"\b(1[0-2][0-9]0)\s+(?:Wien|Vienna)\b", clean_html(card), flags=re.IGNORECASE)
        if not href_match or not title_match or not price_match or not postcode_match:
            continue
        postcode = postcode_match.group(1)
        if postcode not in allowed_postcodes:
            continue
        price_text = price_match.group(1).replace(".", "").replace(",", ".")
        try:
            price = int(round(float(price_text)))
        except ValueError:
            continue
        if price > max_rent:
            continue
        url = html.unescape(href_match.group(1))
        id_match = re.search(r"/detail/(\d+)", url)
        listing_id = id_match.group(1) if id_match else hashlib.sha256(url.encode()).hexdigest()[:16]
        title = clean_html(title_match.group(1))
        contract = extract_contract_term(clean_html(card))
        if contract.label != "not specified" and not contract_is_allowed(contract):
            continue
        size_match = re.search(r"\b(\d+(?:[,.]\d+)?)\s*m²\b", clean_html(card))
        listings.append(
            Listing(
                uid=0,
                fingerprint=listing_fingerprint("oeh", listing_id, price),
                source="ÖH housing board",
                listing_id=listing_id,
                title=title,
                url=url,
                price=price,
                postcode=postcode,
                address=f"{postcode} Wien",
                available="",
                size=f"{size_match.group(1)} m²" if size_match else "",
                priority=classify_priority(title, postcode),
                contract_months=contract.months,
                contract_term=contract.label,
            )
        )
    return listings


def decode_text(value: str | None) -> str:
    if not value:
        return ""
    parts: list[str] = []
    for data, encoding in decode_header(value):
        if isinstance(data, bytes):
            parts.append(data.decode(encoding or "utf-8", errors="replace"))
        else:
            parts.append(data)
    return "".join(parts)


def message_body(message: Message) -> str:
    candidates: list[str] = []
    parts: Iterable[Message] = message.walk() if message.is_multipart() else (message,)
    for part in parts:
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeDecodeError):
            payload = part.get_payload(decode=True) or b""
            content = payload.decode("utf-8", errors="replace")
        if content_type == "text/html":
            parser = TextExtractor()
            parser.feed(str(content))
            content = parser.text()
        candidates.append(str(content))
    return re.sub(r"\s+", " ", " ".join(candidates)).strip()


def extract_prices(text: str) -> tuple[int, ...]:
    patterns = (
        r"(?:€|EUR)\s*([0-9][0-9.,]*)",
        r"([0-9][0-9.,]*)\s*(?:€|EUR)",
    )
    values: set[int] = set()
    for pattern in patterns:
        for value in re.findall(pattern, text, flags=re.IGNORECASE):
            cleaned = value.rstrip(".,")
            separators = [index for index, character in enumerate(cleaned) if character in ".,"]
            if separators and len(cleaned) - separators[-1] - 1 in {1, 2}:
                cleaned = cleaned[: separators[-1]]
            amount_text = re.sub(r"[^0-9]", "", cleaned)
            if not amount_text:
                continue
            amount = int(amount_text)
            if 200 <= amount <= 3000:
                values.add(amount)
    return tuple(sorted(values))


def extract_postcode(text: str) -> str | None:
    matches = re.findall(r"\b(1[0-2][0-9]0)\s+(?:Wien|Vienna)\b", text, flags=re.IGNORECASE)
    return matches[0] if matches else None


def extract_urls(text: str) -> tuple[str, ...]:
    raw_urls = re.findall(r"https?://[^\s<>\"']+", text)
    blocked = ("unsubscribe", "abbestellen", "deaktivieren", "privacy", "datenschutz")
    preferred = ("wg-gesucht", "schwarzesbrett", "immobilien", "willhaben", "home4students")
    clean: list[str] = []
    for url in raw_urls:
        url = url.rstrip(".,;:!?)]")
        lowered = url.lower()
        if any(term in lowered for term in blocked):
            continue
        if any(term in lowered for term in preferred) and url not in clean:
            clean.append(url)
    return tuple(clean[:3])


def classify_priority(text: str, postcode: str | None) -> str:
    lowered = text.lower()
    top_terms = ("1040", "wieden", "schäffergasse", "taubstummengasse", "favoritenstraße", "favoritenstrasse")
    direct_terms = ("hauptbahnhof", "südtiroler platz", "keplerplatz", "reumannplatz", " u1 ")
    if postcode == "1040" or any(term in lowered for term in top_terms):
        return "A — walking-distance candidate"
    if postcode in {"1050", "1100"} or any(term in lowered for term in direct_terms):
        return "B — verify direct route"
    if postcode == "1020":
        return "C — verify U1 proximity"
    return "C — location needs verification"


def parse_alert(uid: int, raw_message: bytes) -> Alert:
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    sender = decode_text(message.get("From"))
    subject = decode_text(message.get("Subject"))
    body = message_body(message)
    combined = f"{subject} {body}"
    message_id = str(message.get("Message-ID") or "")
    fingerprint_source = message_id or f"{sender}\n{subject}\n{body[:2000]}"
    fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8", errors="replace")).hexdigest()
    postcode = extract_postcode(combined)
    contract = extract_contract_term(combined)
    return Alert(
        uid=uid,
        fingerprint=fingerprint,
        sender=sender,
        subject=subject,
        body=body,
        prices=extract_prices(combined),
        postcode=postcode,
        priority=classify_priority(combined, postcode),
        urls=extract_urls(combined),
        contract_months=contract.months,
        contract_term=contract.label,
    )


def is_relevant(alert: Alert, match_terms: tuple[str, ...], max_rent: int) -> bool:
    searchable = f"{alert.sender} {alert.subject} {alert.body}".lower()
    if match_terms and not any(term in searchable for term in match_terms):
        return False
    if not contract_is_allowed(ContractTerm(alert.contract_months, alert.contract_term)):
        return False
    # The smallest plausible amount is normally the monthly rent; larger amounts
    # in an email often represent the deposit. Unknown prices remain visible.
    return not alert.prices or min(alert.prices) <= max_rent


def format_alert(alert: Alert) -> str:
    prices = ", ".join(f"€{price}" for price in alert.prices) if alert.prices else "not detected"
    location = alert.postcode or "not detected"
    links = "\n".join(f'<a href="{html.escape(url, quote=True)}">Open listing {number}</a>' for number, url in enumerate(alert.urls, 1))
    subject = html.escape(alert.subject[:250] or "Apartment alert")
    sender = html.escape(alert.sender[:150])
    return (
        f"<b>{subject}</b>\n"
        f"Priority: {html.escape(alert.priority)}\n"
        f"Price candidates: {prices}\n"
        f"Postcode: {location}\n"
        f"Contract: {html.escape(alert.contract_term)}"
        + (" — ideal" if alert.contract_months == int(os.getenv("IDEAL_CONTRACT_MONTHS", "12")) else "")
        + "\n"
        f"Source: {sender}"
        + (f"\n{links}" if links else "")
    )


def format_listing(listing: Listing) -> str:
    details = [
        f"<b>🏠 {html.escape(listing.source)}: {html.escape(listing.title[:220])}</b>",
        f"Search: {html.escape(listing.search_profile)}",
        f"Priority: {html.escape(listing.priority)}",
        f"Price: €{listing.price}" if listing.price is not None else "Price: not detected",
        f"Location: {html.escape(listing.address or listing.postcode or 'not detected')}",
    ]
    if listing.available:
        details.append(f"Available/published: {html.escape(listing.available)}")
    if listing.size:
        details.append(f"Size: {html.escape(listing.size)}")
    contract = listing.contract_term
    if listing.contract_months == int(os.getenv("IDEAL_CONTRACT_MONTHS", "12")):
        contract += " — ideal"
    details.append(f"Contract: {html.escape(contract)}")
    details.append(f'<a href="{html.escape(listing.url, quote=True)}">Open listing</a>')
    details.append("Verify the exact all-in cost, 08:00 commute, contract and Meldezettel.")
    return "\n".join(details)


def listing_sort_key(listing: Listing) -> tuple[int, float, int, int]:
    priority_order = {"A": 0, "B": 1, "C": 2}
    return (
        priority_order.get(listing.priority[:1], 3),
        listing.distance_km if listing.distance_km is not None else 99.0,
        contract_sort_distance(listing.contract_months),
        listing.price if listing.price is not None else 9999,
    )


def format_listing_batches(listings: list[Listing], max_length: int = 3700) -> list[tuple[str, list[Listing]]]:
    """Create Telegram-sized aggregate messages and retain their state members."""
    heading = f"<b>🏠 {len(listings)} new direct accommodation match(es)</b>\n"
    footer = "\nVerify all-in cost, the 08:00 TU commute, contract and Meldezettel before paying."
    batches: list[tuple[str, list[Listing]]] = []
    lines: list[str] = []
    members: list[Listing] = []
    for listing in sorted(listings, key=listing_sort_key):
        price = f"€{listing.price}" if listing.price is not None else "price unknown"
        location = listing.postcode or listing.address or "location unknown"
        distance = f", ~{listing.distance_km:.1f} km straight-line" if listing.distance_km is not None else ""
        ideal = " — <b>ideal term</b>" if listing.contract_months == int(os.getenv("IDEAL_CONTRACT_MONTHS", "12")) else ""
        line = (
            f'• <a href="{html.escape(listing.url, quote=True)}">'
            f"{html.escape(listing.title[:130])}</a> — {price}, "
            f"{html.escape(location)}, {html.escape(listing.source)}, "
            f"<b>{html.escape(listing.search_profile)}</b>{distance}, "
            f"contract {html.escape(listing.contract_term)}{ideal}"
        )
        candidate = heading + "\n".join(lines + [line]) + footer
        if lines and len(candidate) > max_length:
            batches.append((heading + "\n".join(lines) + footer, members))
            lines = [line]
            members = [listing]
        else:
            lines.append(line)
            members.append(listing)
    if lines:
        batches.append((heading + "\n".join(lines) + footer, members))
    return batches


def filter_listings_by_contract(
    listings: list[Listing], timeout: int, warnings: list[str]
) -> list[Listing]:
    """Resolve missing contract terms from detail pages and enforce the fixed-term range."""
    cache_seconds = max(300, int(os.getenv("CONTRACT_CACHE_SECONDS", "21600")))
    now = time.monotonic()

    def resolve(listing: Listing) -> tuple[Listing, ContractTerm]:
        if listing.contract_term != "not specified":
            return listing, ContractTerm(listing.contract_months, listing.contract_term)
        cached = _CONTRACT_CACHE.get(listing.url)
        if cached and now - cached[0] <= cache_seconds:
            return listing, cached[1]
        detail_text = clean_html(fetch_page(listing.url, timeout))
        term = extract_contract_term(detail_text)
        _CONTRACT_CACHE[listing.url] = (time.monotonic(), term)
        return listing, term

    accepted: list[Listing] = []
    workers = max(1, min(4, int(os.getenv("CONTRACT_FETCH_WORKERS", "4"))))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(resolve, listing): listing for listing in listings}
        for future in as_completed(futures):
            listing = futures[future]
            try:
                resolved_listing, term = future.result()
            except (OSError, ValueError) as error:
                warnings.append(f"Contract check ({listing.source}, {listing.listing_id}): {error}")
                continue
            if not contract_is_allowed(term):
                continue
            accepted.append(
                Listing(
                    **{
                        **resolved_listing.__dict__,
                        "contract_months": term.months,
                        "contract_term": term.label,
                    }
                )
            )
    return accepted


def collect_direct_listings(max_rent: int) -> tuple[list[Listing], list[str]]:
    allowed_postcodes = set(
        env_list("DIRECT_POSTCODES", "1020,1040,1050,1100")
    )
    timeout = int(os.getenv("DIRECT_SOURCE_TIMEOUT", "20"))
    listings: list[Listing] = []
    warnings: list[str] = []
    group_3_budget = int(os.getenv("GROUP_3_MAX_RENT_EUR", "1500"))
    group_2_budget = int(os.getenv("GROUP_2_MAX_RENT_EUR", "1600"))
    highest_budget = max(max_rent, group_3_budget, group_2_budget)

    willhaben_pages = max(1, int(os.getenv("WILLHABEN_MAX_PAGES", "2")))
    for district in WILLHABEN_DISTRICTS:
        for page_number in range(1, willhaben_pages + 1):
            url = (
                f"{WILLHABEN_BASE}{district}/?PRICE_TO={highest_budget}"
                f"&page={page_number}&sort=1"
            )
            try:
                page_listings = parse_willhaben_page(
                    fetch_page(url, timeout), max_rent, allowed_postcodes
                )
                listings.extend(page_listings)
            except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
                warnings.append(f"Willhaben ({district}, page {page_number}): {error}")
                break

    try:
        wg_page = fetch_page(WG_GESUCHT_SEARCH_URL.format(max_rent=max_rent), timeout)
        listings.extend(parse_wg_gesucht_page(wg_page, max_rent, allowed_postcodes))
    except (OSError, ValueError) as error:
        warnings.append(f"WG-Gesucht: {error}")

    try:
        apartment_page = fetch_page(
            WG_GESUCHT_APARTMENT_URL.format(max_rent=highest_budget), timeout
        )
        listings.extend(
            parse_wg_gesucht_page(
                apartment_page,
                highest_budget,
                allowed_postcodes,
                whole_apartments=True,
            )
        )
    except (OSError, ValueError) as error:
        warnings.append(f"WG-Gesucht whole apartments: {error}")

    try:
        listings.extend(parse_oeh_page(fetch_page(OEH_SEARCH_URL, timeout), max_rent, allowed_postcodes))
    except (OSError, ValueError) as error:
        warnings.append(f"ÖH housing board: {error}")

    # A source can repeat promoted listings. Keep only one copy per fingerprint.
    unique = {listing.fingerprint: listing for listing in listings}
    filtered = filter_listings_by_contract(list(unique.values()), timeout, warnings)
    return filtered, warnings


class State:
    def __init__(self, path: Path) -> None:
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS processed "
            "(fingerprint TEXT PRIMARY KEY, uid INTEGER NOT NULL, processed_at TEXT NOT NULL)"
        )
        self.connection.commit()

    def contains(self, fingerprint: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM processed WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        return row is not None

    def add(self, alert: Alert | Listing) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO processed(fingerprint, uid, processed_at) VALUES (?, ?, ?)",
            (alert.fingerprint, alert.uid, datetime.now(timezone.utc).isoformat()),
        )
        self.connection.commit()


def fetch_messages(config: dict[str, str], lookback_days: int) -> list[tuple[int, bytes]]:
    host = config["IMAP_HOST"]
    port = int(config.get("IMAP_PORT", "993"))
    mailbox = config.get("IMAP_FOLDER", "INBOX")
    since = (datetime.now() - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
    # A long-running bot must not remain stuck forever when an established IMAP
    # connection stops responding. The outer loop can retry after this timeout.
    with imaplib.IMAP4_SSL(host, port, timeout=20) as client:
        client.login(config["IMAP_USER"], config["IMAP_PASSWORD"])
        status, _ = client.select(mailbox, readonly=True)
        if status != "OK":
            raise RuntimeError(f"Could not open IMAP folder {mailbox!r}")
        status, data = client.uid("search", None, f'(SINCE "{since}")')
        if status != "OK":
            raise RuntimeError("IMAP search failed")
        messages: list[tuple[int, bytes]] = []
        for raw_uid in (data[0] or b"").split():
            status, fetched = client.uid("fetch", raw_uid, "(BODY.PEEK[])")
            if status != "OK" or not fetched or not isinstance(fetched[0], tuple):
                continue
            messages.append((int(raw_uid), fetched[0][1]))
        return messages


def send_telegram(token: str, chat_id: str, text: str) -> None:
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urlencode(
        {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
    ).encode("utf-8")
    request = Request(endpoint, data=payload, method="POST")
    with urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(f"Telegram rejected the message: {result}")


def required_config() -> dict[str, str]:
    required = ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Missing configuration: {', '.join(missing)}. Copy .env.example to .env.")
    return {name: value for name in required + ("IMAP_PORT", "IMAP_FOLDER") if (value := os.getenv(name))}


def build_self_test_email() -> bytes:
    message = EmailMessage()
    message["From"] = "WG-Gesucht Self-Test <alerts@wg-gesucht.de>"
    message["To"] = "apartment-bot@example.invalid"
    message["Subject"] = "SELF-TEST: WG-Zimmer near TU Wien"
    message["Message-ID"] = f"<self-test-{time.time_ns()}@notification-bot.local>"
    message.set_content(
        "Synthetic listing for €650 per month with a 12-month contract in 1040 Wien, close to "
        "Favoritenstraße 9–11. https://www.wg-gesucht.de/self-test.html"
    )
    return message.as_bytes()


def run_self_test() -> None:
    """Test configuration, IMAP, parsing/filtering and Telegram delivery."""
    config = required_config()
    max_rent = int(os.getenv("MAX_RENT_EUR", "700"))
    match_terms = env_list(
        "ALERT_MATCH_TERMS",
        "wg-gesucht,schwarzes brett,schwarzesbrett,visualping,distill,wohnung,wg-zimmer",
    )

    print("[1/4] Configuration: PASS")
    host = config["IMAP_HOST"]
    port = int(config.get("IMAP_PORT", "993"))
    mailbox = config.get("IMAP_FOLDER", "INBOX")
    with imaplib.IMAP4_SSL(host, port, timeout=20) as client:
        client.login(config["IMAP_USER"], config["IMAP_PASSWORD"])
        status, _ = client.select(mailbox, readonly=True)
        if status != "OK":
            raise RuntimeError(f"Could not open IMAP folder {mailbox!r}")
    print(f"[2/4] Gmail IMAP read-only access to {mailbox!r}: PASS")

    alert = parse_alert(0, build_self_test_email())
    checks = {
        "price": 650 in alert.prices,
        "postcode": alert.postcode == "1040",
        "priority": alert.priority.startswith("A"),
        "listing URL": bool(alert.urls),
        "filter": is_relevant(alert, match_terms, max_rent),
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise RuntimeError(f"Synthetic alert failed: {', '.join(failed)}")
    print("[3/4] Email parser, budget, 6–24 month contract, priority and link extraction: PASS")

    telegram_text = "<b>✅ APARTMENT BOT SELF-TEST</b>\n" + format_alert(alert)
    send_telegram(config["TELEGRAM_BOT_TOKEN"], config["TELEGRAM_CHAT_ID"], telegram_text)
    print("[4/4] Telegram delivery: PASS")
    print("SELF-TEST PASSED: the notification pipeline works in principle.")


def run_once(state: State, dry_run: bool = False, scan_direct: bool = True) -> int:
    config = required_config()
    max_rent = int(os.getenv("MAX_RENT_EUR", "700"))
    lookback_days = int(os.getenv("LOOKBACK_DAYS", "3"))
    match_terms = env_list(
        "ALERT_MATCH_TERMS",
        "wg-gesucht,schwarzes brett,schwarzesbrett,visualping,distill,wohnung,wg-zimmer",
    )
    alerts: list[Alert] = []
    for uid, raw_message in fetch_messages(config, lookback_days):
        alert = parse_alert(uid, raw_message)
        if state.contains(alert.fingerprint):
            continue
        if is_relevant(alert, match_terms, max_rent):
            alerts.append(alert)
        else:
            state.add(alert)

    direct_listings: list[Listing] = []
    if scan_direct and env_bool("ENABLE_DIRECT_SEARCH", True):
        collected, warnings = collect_direct_listings(max_rent)
        for warning in warnings:
            print(f"DIRECT SOURCE WARNING: {warning}", file=sys.stderr)
        direct_listings = [listing for listing in collected if not state.contains(listing.fingerprint)]

    if not alerts and not direct_listings:
        suffix = " (email and direct sources)." if scan_direct else " (email)."
        print("No new matching apartment alerts" + suffix)
        return 0

    for alert in alerts:
        output = format_alert(alert)
        if dry_run:
            print(output)
            print("---")
        else:
            send_telegram(config["TELEGRAM_BOT_TOKEN"], config["TELEGRAM_CHAT_ID"], output)
        # Record a matching alert only after it was printed or delivered. A
        # transient Telegram error therefore cannot silently lose a listing.
        state.add(alert)

    for output, batch_members in format_listing_batches(direct_listings):
        if dry_run:
            print(output)
            print("---")
        else:
            send_telegram(config["TELEGRAM_BOT_TOKEN"], config["TELEGRAM_CHAT_ID"], output)
        for listing in batch_members:
            state.add(listing)

    total = len(alerts) + len(direct_listings)
    print(
        f"Processed {total} new match(es): {len(alerts)} email, "
        f"{len(direct_listings)} direct-source."
    )
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Check once and exit")
    parser.add_argument("--dry-run", action="store_true", help="Print alerts instead of sending Telegram messages")
    parser.add_argument(
        "--email-only",
        action="store_true",
        help="Skip direct Willhaben, WG-Gesucht and ÖH searches",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Test Gmail, parsing, filtering and Telegram with a synthetic listing",
    )
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env", help="Configuration file")
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE, help="SQLite state file")
    args = parser.parse_args()

    load_env_file(args.env_file)

    if args.self_test:
        run_self_test()
        return 0

    state = State(args.state_file)
    poll_seconds = max(30, int(os.getenv("POLL_SECONDS", "60")))

    if args.once or args.dry_run:
        run_once(state, dry_run=args.dry_run, scan_direct=not args.email_only)
        return 0

    print(f"Apartment alert bot started; checking every {poll_seconds} seconds.")
    direct_scan_seconds = max(300, int(os.getenv("DIRECT_SCAN_SECONDS", "900")))
    next_direct_scan = 0.0
    try:
        while True:
            try:
                now = time.monotonic()
                scan_direct = not args.email_only and now >= next_direct_scan
                run_once(state, scan_direct=scan_direct)
                if scan_direct:
                    next_direct_scan = now + direct_scan_seconds
            except (OSError, RuntimeError, imaplib.IMAP4.error) as error:
                print(f"{datetime.now().isoformat(timespec='seconds')} ERROR: {error}", file=sys.stderr)
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        print("Apartment alert bot stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
