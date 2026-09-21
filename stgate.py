import asyncio
import json
import os
import re
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, quote, urlparse

import httpx
from playwright.async_api import async_playwright, Browser
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

# --------------------------------------------------
# CONFIG
# --------------------------------------------------

TAG = "STGATE"

BASE_URL = os.environ.get("STGATE_BASE_URL")
if not BASE_URL:
    raise RuntimeError("Missing STGATE_BASE_URL secret")

SPORT_ENDPOINTS = [
    "cfb",
    "mlb",
    "nba",
    "nfl",
    "nhl",
    "soccer",
    "ufc",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:146.0) Gecko/20100101 Firefox/146.0"
)

UA_ENC = quote(USER_AGENT)

OUT_VLC = Path("stgate_vlc.m3u8")
OUT_TIVI = Path("stgate_tivimate.m3u8")

CACHE_FILE = Cache(TAG, exp=10_800)
API_FILE = Cache(f"{TAG}-api", exp=19_800)

urls: dict[str, dict[str, Any]] = {}

# --------------------------------------------------
# Regex patterns
# --------------------------------------------------

# Working patterns
VALID_M3U8 = re.compile(
    r"(file|source|streamurls?)\s*(:|=)\s+(\'|\")([^\"]*)(\'|\")",
    re.I,
)

VALID_M3U8_2 = re.compile(
    r"(streamurls|0x31c4)\s?=\s?\[\s*[\"']([^\"']+)[\"']",
    re.I,
)

DIRECT_M3U8 = re.compile(
    r"""https?://[^\s'"<>()]+?/live/[^\s'"<>()]+?\.m3u8[^\s'"<>()]*""",
    re.I,
)

VALID_M3U8_ARRAY = re.compile(
    r"""(?:streamurls|sources|0x31c4)\s*[:=]\s*\[\s*['"]([^'"]+)['"]""",
    re.I,
)


# --------------------------------------------------
def clean_m3u(s: str) -> str:
    return re.sub(r"\.live(?=/|\?|$)", ".pro", s)


def unescape_js_string(raw: str) -> str:
    """Decode JS escapes like \\u0026, \\/, \\', \\"."""
    try:
        return json.loads(f'"{raw}"')
    except (json.JSONDecodeError, IndexError):
        return (
            raw.replace("\\u0026", "&")
            .replace("\\u0026amp;", "&")
            .replace("\\/", "/")
            .replace("\\'", "'")
            .replace('\\"', '"')
        )


def force_full_token(url: str) -> str:
    url = url.strip().rstrip("\\").rstrip("&").rstrip("?")
    if "?st=" in url and "&e=" not in url and "\\u0026" not in url:
        log.warning(f"Token incomplete (no &e=): {url}")
    return url


def extract_stream_id(stream_url: str) -> str | None:
    if not stream_url:
        return None

    patterns = [
        r"/live/([^/]+)/[^/]*\.m3u8",
        r"/US/([^/]+)/index\.m3u8",
        r"/([A-Z0-9]+)/index\.m3u8",
        r"stream=([A-Z0-9]+)",
        r"/stream/([A-Z0-9]+)\.m3u8",
    ]

    for pattern in patterns:
        match = re.search(pattern, stream_url, re.IGNORECASE)
        if match:
            return match.group(1)

    return None


def build_referer_from_stream(stream_url: str) -> str:
    stream_id = extract_stream_id(stream_url)

    if stream_id:
        return f"https://instream.click/livetv.php?stream={stream_id}"

    parsed = urlparse(stream_url)
    if parsed.path:
        parts = parsed.path.split("/")
        if len(parts) > 2 and parts[1].upper() in ["US", "CA", "UK"]:
            return f"https://instream.click/livetv.php?stream={parts[2]}"

    return "https://instream.click/"


def extract_m3u8_with_token(text: str) -> str | None:
    # 1. Direct grab — this now includes `\u0026e=...` in the match
    if match := DIRECT_M3U8.search(text):
        url = unescape_js_string(match.group(0)).strip()
        url = force_full_token(url)
        if ".m3u8" in url:
            return url

    # 2. Array-style
    for pattern in (VALID_M3U8_ARRAY, VALID_M3U8_2):
        if match := pattern.search(text):
            raw = match.group(1) if pattern is VALID_M3U8_ARRAY else match.group(2)
            url = unescape_js_string(raw).strip()
            url = force_full_token(url)
            if ".m3u8" in url:
                return url

    # 3. Named-key
    if match := VALID_M3U8.search(text):
        raw = match.group(4)
        url = unescape_js_string(raw).strip()
        url = force_full_token(url)
        if ".m3u8" in url:
            return url

    return None


# --------------------------------------------------
async def process_event(url: str, url_num: int) -> tuple[str | None, str | None]:
    """Extract M3U8 stream URL (with full st+e token) and iframe referer."""
    nones = None, None

    if not (
        event_data := await network.request(
            url,
            url_num,
            headers={"Referer": BASE_URL},
            timeout=httpx.Timeout(25.0),
            log=log,
        )
    ):
        return nones

    soup = HTMLParser(event_data.content)

    ifr = soup.css_first("iframe")

    if not ifr or not (src := ifr.attributes.get("src")):
        log.warning(f"URL {url_num}) No iframe element found.")
        return nones

    ifr_src = network.ensure_https(src)

    if not (
        ifr_src_data := await network.request(
            ifr_src,
            url_num,
            headers={"Referer": url},
            log=log,
        )
    ):
        return nones

    if stream_url := extract_m3u8_with_token(ifr_src_data.text):
        stream_url = re.sub(r"[\\'\"<>)\s]+$", "", stream_url)
        log.info(f"URL {url_num}) Captured M3U8")
        return stream_url, ifr_src

    log.warning(f"URL {url_num}) No source found.")
    return nones


# --------------------------------------------------
async def refresh_api_cache(now: Time) -> list[dict[str, Any]]:
    log.info("Refreshing API cache")

    tasks = [
        network.request(
            urljoin(BASE_URL, "api/v1/games.php"),
            params={"sport": sport, "limit": 100},
            timeout=httpx.Timeout(25.0),
            log=log,
        )
        for sport in SPORT_ENDPOINTS
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    data: list[dict[str, Any]] = []

    for sport, r in zip(SPORT_ENDPOINTS, results):
        if isinstance(r, Exception):
            log.warning(f"{sport} → request failed: {str(r)[:50]}")
            continue

        if not r:
            continue

        try:
            payload = r.json()
        except Exception as e:
            log.warning(f"{sport} → invalid JSON: {str(e)[:50]}")
            continue

        items = payload.get("data") if isinstance(payload, dict) else payload
        if not items:
            continue

        log.info(f"{sport} → {len(items)} events")
        data.extend(items)

    if not data:
        return [{"timestamp": now.timestamp()}]

    data[-1]["timestamp"] = now.timestamp()
    return data


# --------------------------------------------------
async def get_events(cached_keys: list[str]) -> list[dict[str, Any]]:
    now = Time.rn()

    if not (api_data := API_FILE.load(per_entry=False, ts_index=-1)):
        api_data = await refresh_api_cache(now)
        API_FILE.write(api_data)

    events: list[dict[str, Any]] = []

    # Expanded window (was -3h → +30min, now -48h → +12h)
    start_dt = now.delta(hours=-48)
    end_dt = now.delta(hours=12)

    seen_events: set[str] = set()

    for stream_group in api_data:
        if not all(
            values := [
                stream_group.get(x)
                for x in (
                    "league",
                    "start_at",
                    "away",
                    "home",
                    "streams",
                )
            ]
        ):
            continue

        sport, event_time, away, home, streams = values

        try:
            event_dt = Time.fromisoformat(event_time).to_tz("EST")
        except Exception:
            continue

        if not start_dt <= event_dt <= end_dt:
            continue

        if not (home_team := home.get("name")):
            continue

        name = (
            f"{away_team} vs {home_team}"
            if (away_team := away.get("name")) and away_team != home_team
            else home_team
        )

        sport = str(sport).strip()

        stream_urls: dict[str, str | None] = {
            stream.get("label") or "English": stream.get("url") for stream in streams
        }

        for lang, s_url in stream_urls.items():
            if not s_url:
                continue

            key = f"[{sport}] {name} | {lang} ({TAG})"
            if key in cached_keys:
                continue

            event_id = f"{sport}_{name}_{lang}"
            if event_id in seen_events:
                continue
            seen_events.add(event_id)

            events.append(
                {
                    "sport": sport,
                    "event": f"{name} | {lang}",
                    "link": urljoin(BASE_URL, s_url),
                    "timestamp": now.timestamp(),
                }
            )

    log.info(f"Found {len(events)} new events to process")
    return events


# --------------------------------------------------
async def scrape(browser: Browser) -> None:
    cached_urls = CACHE_FILE.load() or {}
    cached_count = len(cached_urls)

    urls.update(cached_urls)

    log.info(f"Loaded {cached_count} event(s) from cache")
    log.info(f'Scraping from "{BASE_URL}"')

    events = await get_events(list(cached_urls.keys()))

    if not events:
        log.info("No new events found")
        build_playlists(cached_urls)
        return

    log.info(f"Processing {len(events)} new URL(s)")

    processed_count = 0
    failed_count = 0

    for i, ev in enumerate(events, start=1):
        try:
            handler = partial(
                process_event,
                url=ev["link"],
                url_num=i,
            )

            source, iframe = await network.safe_process(
                handler,
                url_num=i,
                timeout_return=(None, None),
                semaphore=network.HTTP_S,
                log=log,
            )

            key = f"[{ev['sport']}] {ev['event']} ({TAG})"
            tvg_id, logo = leagues.get_tvg_info(ev["sport"], ev["event"])

            entry = {
                "url": source,
                "logo": logo,
                "sport": ev["sport"],
                "base": BASE_URL,
                "timestamp": ev["timestamp"],
                "id": tvg_id or "Live.Event.us",
                "link": ev["link"],
                "referer": iframe or (build_referer_from_stream(source) if source else None),
            }

            cached_urls[key] = entry

            if source:
                entry["url"] = clean_m3u(source)
                urls[key] = entry
                processed_count += 1
            else:
                failed_count += 1

        except Exception as e:
            log.error(f"Error processing event {i}: {str(e)[:100]}")
            failed_count += 1
            continue

    CACHE_FILE.write(cached_urls)
    build_playlists(cached_urls)

    log.info(f"Successfully processed {processed_count} new event(s)")
    if failed_count:
        log.warning(f"Failed to process {failed_count} event(s)")


# --------------------------------------------------
def build_playlists(data: dict[str, dict]) -> None:
    # Only include entries that actually have a stream url
    valid = {k: v for k, v in data.items() if v.get("url")}

    if not valid:
        log.warning("No data to build playlists")
        OUT_VLC.write_text("#EXTM3U\n", encoding="utf-8")
        OUT_TIVI.write_text("#EXTM3U\n", encoding="utf-8")
        return

    vlc = ["#EXTM3U"]
    tm = ["#EXTM3U"]
    ch = 1

    sorted_items = sorted(
        valid.items(), key=lambda x: (x[1].get("sport", "ZZZ"), x[0])
    )

    for name, e in sorted_items:
        stream_url = e["url"]

        referer = e.get("referer") or build_referer_from_stream(stream_url)

        vlc_lines = [
            f'#EXTINF:-1 tvg-chno="{ch}" tvg-id="{e["id"]}" '
            f'tvg-name="{name}" tvg-logo="{e["logo"]}" group-title="Live Events",{name}',
            f"#EXTVLCOPT:http-referrer={referer}",
            f"#EXTVLCOPT:http-origin={referer}",
            f"#EXTVLCOPT:http-user-agent={USER_AGENT}",
            stream_url,
            "",
        ]
        vlc.extend(vlc_lines)

        tm_lines = [
            f'#EXTINF:-1 tvg-chno="{ch}" tvg-id="{e["id"]}" '
            f'tvg-name="{name}" tvg-logo="{e["logo"]}" group-title="Live Events",{name}',
            f"{stream_url}|referer={referer}|origin={referer}|user-agent={UA_ENC}",
            "",
        ]
        tm.extend(tm_lines)

        ch += 1

    OUT_VLC.write_text("\n".join(vlc), encoding="utf-8")
    OUT_TIVI.write_text("\n".join(tm), encoding="utf-8")

    log.info(f"Playlists written successfully with {ch - 1} channels")
    log.info(f"  - {OUT_VLC}")
    log.info(f"  - {OUT_TIVI}")


# --------------------------------------------------
async def main() -> None:
    log.info("Starting STGATE updater")
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--autoplay-policy=no-user-gesture-required",
                    "--disable-web-security",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            await scrape(browser)
            await browser.close()
    except Exception as e:
        log.error(f"Fatal error in main: {str(e)[:200]}")
        cached_urls = CACHE_FILE.load() or {}
        if cached_urls:
            build_playlists(cached_urls)
        raise


# --------------------------------------------------
if __name__ == "__main__":
    asyncio.run(main())
