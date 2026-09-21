import asyncio
import json
import os
import re
import httpx
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, quote, urlparse

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

# New sports endpoints (JSON files under /data-cache/)
SPORT_ENDPOINTS: list[tuple[str, str, str]] = [
    ("basketball", "matches-basketball.json", "Basketball"),
    ("football", "matches-football.json", "Football"),
    ("american-football", "matches-american-football.json", "American Football"),
    ("hockey", "matches-hockey.json", "Hockey"),
    ("baseball", "matches-baseball.json", "Baseball"),
    ("motor-sports", "matches-motor-sports.json", "Motor Sport"),
    ("fight", "matches-fight.json", "Fight MMA"),
    ("tennis", "matches-tennis.json", "Tennis"),
    ("rugby", "matches-rugby.json", "Rugby"),
    ("golf", "matches-golf.json", "Golf"),
    ("billiards", "matches-billiards.json", "Billiards"),
    ("afl", "matches-afl.json", "AFL"),
    ("darts", "matches-darts.json", "Darts"),
    ("cricket", "matches-cricket.json", "Cricket"),
    ("other", "matches-other.json", "Other"),
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

VALID_M3U8 = re.compile(
    r"""(?:file|source|streamurls?|stream_url|url)\s*[:=]\s*['"]([^'"]+)['"]""",
    re.I,
)

VALID_M3U8_ARRAY = re.compile(
    r"""(?:streamurls|sources|0x31c4)\s*[:=]\s*\[\s*['"]([^'"]+)['"]""",
    re.I,
)

VALID_M3U8_ALT = re.compile(
    r"""(?:file|source|streamurls?)\s*(?::|=)\s*(?:'|")([^"']*)(?:'|")""",
    re.I,
)

DIRECT_M3U8 = re.compile(
    r"""https?://instreams?\.(?:live|pro|click|xyz|tv|st)/live/[^\s'"\\<>)]+?\.m3u8[^\s'"\\<>)]*""",
    re.I,
)


# --------------------------------------------------
# Timestamp helpers (robust parsing)
# --------------------------------------------------

def parse_iso_timestamp(value: Any) -> float | None:
    """Parse an ISO-8601 timestamp (with Z or +00:00) to a Unix timestamp.

    Returns None on failure. Handles both numeric and string inputs.
    """
    if value is None:
        return None

    if isinstance(value, (int, float)):
        # Could be seconds or milliseconds — normalize
        v = float(value)
        if v > 1e12:  # milliseconds
            v /= 1000.0
        return v

    s = str(value).strip()
    if not s:
        return None

    # Try numeric string first
    try:
        v = float(s)
        if v > 1e12:
            v /= 1000.0
        return v
    except ValueError:
        pass

    # ISO-8601 with trailing Z
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


# --------------------------------------------------
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


def get_event(t1: str, t2: str) -> str:
    if t1 == "RED ZONE":
        return "NFL RedZone"
    if t1 == "TBD":
        return "TBD"
    return f"{t1.strip()} vs {t2.strip()}"


def clean_sport_name(sport: str) -> str:
    sport_map = {
        "soccer": "Football",
        "football": "Football",
        "nfl": "American Football",
        "american-football": "American Football",
        "nba": "Basketball",
        "basketball": "Basketball",
        "cfb": "NCAA Football",
        "mlb": "Baseball",
        "baseball": "Baseball",
        "nhl": "Hockey",
        "hockey": "Hockey",
        "ufc": "Fight MMA",
        "fight": "Fight MMA",
        "box": "Boxing",
        "f1": "Motor Sport",
        "motor-sports": "Motor Sport",
        "tennis": "Tennis",
        "rugby": "Rugby",
        "golf": "Golf",
        "billiards": "Billiards",
        "afl": "AFL",
        "darts": "Darts",
        "cricket": "Cricket",
        "other": "Other",
        "olympics": "Olympics",
    }
    return sport_map.get(sport.lower(), sport)


def clean_m3u(s: str) -> str:
    return re.sub(r"[\r\n]+$", "", s)


def unescape_js_string(raw: str) -> str:
    try:
        return json.loads(f'"{raw}"')
    except (json.JSONDecodeError, IndexError):
        return (
            raw.replace("\\u0026", "&")
            .replace("\\/", "/")
            .replace("\\'", "'")
            .replace('\\"', '"')
        )


def extract_m3u8_with_token(text: str) -> str | None:
    if match := DIRECT_M3U8.search(text):
        return unescape_js_string(match.group(0)).strip()

    for pattern in (VALID_M3U8_ARRAY, VALID_M3U8, VALID_M3U8_ALT):
        if match := pattern.search(text):
            url = unescape_js_string(match.group(1)).strip()
            if ".m3u8" in url:
                return url

    return None


# --------------------------------------------------
async def process_event(url: str, url_num: int) -> tuple[str | None, str | None]:
    nones = None, None

    if not (event_data := await network.request(url, url_num, log=log)):
        return nones

    if re.search(r"^https?://instreams?", url.lower()):
        ifr_src, ifr_src_data_text = url, event_data.text
    else:
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

        ifr_src_data_text = ifr_src_data.text

    if stream_url := extract_m3u8_with_token(ifr_src_data_text):
        stream_url = re.sub(r"[\\'\"<>)\s]+$", "", stream_url)
        log.info(f"URL {url_num}) Captured M3U8 (with token)")
        return stream_url, ifr_src

    log.warning(f"URL {url_num}) No source found.")
    return nones


# --------------------------------------------------
async def refresh_api_cache(now_ts: float) -> list[dict[str, Any]]:
    log.info("Refreshing JSON API cache")

    tasks = [
        network.request(
            urljoin(BASE_URL, f"data-cache/{filename}"),
            timeout=httpx.Timeout(25.0),
            log=log,
        )
        for _, filename, _ in SPORT_ENDPOINTS
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    data: list[dict[str, Any]] = []

    for (sport_key, filename, canonical_sport), r in zip(SPORT_ENDPOINTS, results):
        if isinstance(r, Exception):
            log.warning(f"{filename} → request failed: {str(r)[:50]}")
            continue

        if not r:
            continue

        try:
            payload = r.json()
        except Exception as e:
            log.warning(f"{filename} → invalid JSON: {str(e)[:50]}")
            continue

        # New format: {"sport_name": ..., "items": [...]}
        if isinstance(payload, dict):
            items = payload.get("items") or []
            sport_name = payload.get("sport_name") or canonical_sport
        elif isinstance(payload, list):
            items = payload
            sport_name = canonical_sport
        else:
            continue

        log.info(f"{filename} → {len(items)} events")

        for ev in items:
            # Normalize to a single `ts` field (seconds, float)
            ts_value = (
                ev.get("scheduled_at")
                or ev.get("timestamp")
                or ev.get("time")
                or ev.get("ts")
            )
            ts_parsed = parse_iso_timestamp(ts_value)
            if ts_parsed is not None:
                ev["ts"] = ts_parsed
            ev["_sport"] = clean_sport_name(sport_name)
            ev["_sport_key"] = sport_key

        data.extend(items)

    if not data:
        return [{"timestamp": now_ts}]

    # Sentinel entry — do NOT give it any other required keys so it's skipped
    data.append({"timestamp": now_ts})
    return data


# --------------------------------------------------
async def get_events(cached_keys: list[str]) -> list[dict[str, Any]]:
    now = Time.rn()

    api_data = API_FILE.load(per_entry=False, ts_index=-1)
    if not api_data:
        log.info("Refreshing API cache")
        api_data = await refresh_api_cache(now.timestamp())
        API_FILE.write(api_data)

    events: list[dict[str, Any]] = []

    # Widen window slightly to catch events whose timestamps were parsed as UTC
    # but whose local "now" differs.
    start_dt = now.delta(hours=-48)
    end_dt = now.delta(hours=12)

    seen_events: set[str] = set()
    skipped_no_ts = 0
    skipped_no_sport = 0
    skipped_no_title = 0
    skipped_window = 0
    skipped_no_streams = 0
    skipped_cached = 0

    for ev in api_data:
        # Skip sentinel entries
        if "ts" not in ev and "title" not in ev and "home_team" not in ev:
            continue

        ts_value = ev.get("ts")
        sport = ev.get("_sport")
        title = ev.get("title") or ev.get("name")
        home = ev.get("home_team")
        away = ev.get("away_team")

        if ts_value is None:
            skipped_no_ts += 1
            continue

        if not sport:
            skipped_no_sport += 1
            continue

        if not title and not (home and away):
            skipped_no_title += 1
            continue

        # Build event name
        if home and away:
            event = get_event(home, away)
        else:
            event = title.strip()

        # Parse timestamp robustly (in case cache holds a string)
        event_ts = parse_iso_timestamp(ts_value)
        if event_ts is None:
            skipped_no_ts += 1
            continue

        try:
            event_dt = Time.from_ts(event_ts)
        except Exception:
            skipped_no_ts += 1
            continue

        if not start_dt <= event_dt <= end_dt:
            skipped_window += 1
            continue

        key = f"[{sport}] {event} ({TAG})"
        if key in cached_keys:
            skipped_cached += 1
            continue

        event_id = str(ev.get("id") or f"{sport}_{title}_{event_ts}")
        if event_id in seen_events:
            continue
        seen_events.add(event_id)

        # Collect embed URLs from sources
        sources = ev.get("sources") or []
        stream_urls: list[str] = []
        for src in sources:
            embed_url = src.get("embed_url")
            if embed_url:
                stream_urls.append(embed_url)

        if not stream_urls:
            skipped_no_streams += 1
            continue

        events.append(
            {
                "sport": sport,
                "event": event,
                "link": stream_urls[0],
                "timestamp": event_ts,
                "stream_count": len(stream_urls),
                "all_streams": stream_urls,
            }
        )

    log.info(
        f"Filter diagnostics: no_ts={skipped_no_ts} "
        f"no_sport={skipped_no_sport} no_title={skipped_no_title} "
        f"out_of_window={skipped_window} no_streams={skipped_no_streams} "
        f"cached={skipped_cached}"
    )

    events.sort(key=lambda x: x["timestamp"], reverse=True)

    log.info(f"Found {len(events)} new events to process")
    return events


# --------------------------------------------------
async def scrape(browser: Browser) -> None:
    cached_urls = CACHE_FILE.load() or {}
    cached_count = len(cached_urls)

    urls.update(cached_urls)

    log.info(f"Loaded {cached_count} cached event(s)")
    log.info(f'Scraping JSON from "{BASE_URL}/data-cache"')

    events = await get_events(list(cached_urls.keys()))

    if not events:
        log.info("No new events found")
        build_playlists(cached_urls)
        return

    log.info(f"Processing {len(events)} new stream URL(s)")

    processed_count = 0
    failed_count = 0

    for i, ev in enumerate(events, start=1):
        try:
            handler = partial(
                process_event,
                url=ev["link"],
                url_num=i,
            )

            stream_url, iframe_src = await network.safe_process(
                handler,
                url_num=i,
                timeout_return=(None, None),
                semaphore=network.HTTP_S,
                log=log,
            )

            if not stream_url and ev.get("all_streams") and len(ev["all_streams"]) > 1:
                log.info(f"Trying fallback streams for {ev['event']}")
                for fallback_url in ev["all_streams"][1:3]:
                    fallback_handler = partial(
                        process_event,
                        url=fallback_url,
                        url_num=i,
                    )
                    stream_url, iframe_src = await network.safe_process(
                        fallback_handler,
                        url_num=i,
                        timeout_return=(None, None),
                        semaphore=network.HTTP_S,
                        log=log,
                    )
                    if stream_url:
                        log.info(f"Fallback stream successful for {ev['event']}")
                        break

            if not stream_url:
                failed_count += 1
                continue

            referer = iframe_src or build_referer_from_stream(stream_url)

            key = f"[{ev['sport']}] {ev['event']} ({TAG})"
            tvg_id, logo = leagues.get_tvg_info(ev["sport"], ev["event"])

            full_stream_url = clean_m3u(stream_url)

            cached_urls[key] = {
                "url": full_stream_url,
                "logo": logo,
                "base": BASE_URL,
                "sport": ev["sport"],
                "timestamp": ev["timestamp"],
                "id": tvg_id or "Live.Event.us",
                "link": ev["link"],
                "referer": referer,
                "stream_count": ev.get("stream_count", 1),
            }

            processed_count += 1

        except Exception as e:
            log.error(f"Error processing event {i}: {str(e)[:100]}")
            failed_count += 1
            continue

    if cached_urls:
        now = Time.rn()
        expired_keys = [
            key
            for key, data in cached_urls.items()
            if data.get("timestamp", 0) < now.delta(hours=-48).timestamp()
        ]

        if expired_keys:
            log.info(f"Removing {len(expired_keys)} expired cache entries")
            for key in expired_keys:
                del cached_urls[key]

    CACHE_FILE.write(cached_urls)
    build_playlists(cached_urls)

    log.info(f"Successfully processed {processed_count} new event(s)")
    if failed_count > 0:
        log.warning(f"Failed to process {failed_count} event(s)")


# --------------------------------------------------
def build_playlists(data: dict[str, dict]) -> None:
    if not data:
        log.warning("No data to build playlists")
        OUT_VLC.write_text("#EXTM3U\n", encoding="utf-8")
        OUT_TIVI.write_text("#EXTM3U\n", encoding="utf-8")
        return

    vlc = ["#EXTM3U"]
    tm = ["#EXTM3U"]
    ch = 1

    sorted_items = sorted(
        data.items(), key=lambda x: (x[1].get("sport", "ZZZ"), x[0])
    )

    for name, e in sorted_items:
        stream_url = clean_m3u(e["url"])

        referer = e.get("referer")
        if not referer:
            referer = build_referer_from_stream(stream_url)

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
