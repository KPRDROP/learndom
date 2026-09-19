import asyncio
import json
import os
import re
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

BASE_URL = os.environ.get("STGATE_BASE_URL") or "https://streamsgates.pk"
if not BASE_URL:
    raise RuntimeError("Missing STGATE_BASE_URL secret")

# New sports endpoints (JSON files under /data-cache/)
# Each tuple: (sport_key, json_filename, canonical_sport_name)
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

# Named-key patterns (file: "..." | source = '...' | streamurls: "...")
VALID_M3U8 = re.compile(
    r"""(?:file|source|streamurls?|stream_url|url)\s*[:=]\s*['"]([^'"]+)['"]""",
    re.I,
)

# Array style: streamurls = ["URL"] | sources: ["URL"] | 0x31c4 = ["URL"]
VALID_M3U8_ARRAY = re.compile(
    r"""(?:streamurls|sources|0x31c4)\s*[:=]\s*\[\s*['"]([^'"]+)['"]""",
    re.I,
)

# Alternate fallback
VALID_M3U8_ALT = re.compile(
    r"""(?:file|source|streamurls?)\s*(?::|=)\s*(?:'|")([^"']*)(?:'|")""",
    re.I,
)

# Direct URL match — most reliable, grabs the raw URL from the player source
DIRECT_M3U8 = re.compile(
    r"""https?://instreams?\.(?:live|pro|click|xyz|tv|st)/live/[^\s'"\\<>)]+?\.m3u8[^\s'"\\<>)]*""",
    re.I,
)

# --------------------------------------------------
def extract_stream_id(stream_url: str) -> str | None:
    """Extract stream ID from an M3U8 URL or player URL."""
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
    """Build the correct referer URL based on stream URL."""
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
    """Clean and standardize sport names."""
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
    """Remove trailing newlines but PRESERVE query tokens (st, e, etc.)."""
    return re.sub(r"[\r\n]+$", "", s)


def unescape_js_string(raw: str) -> str:
    """Unescape JS string escapes like \\u0026, \\/, \\', etc."""
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
    """Extract M3U8 URL including the full query string (st=..., e=...)."""
    # 1. Direct URL match — grabs the raw URL anywhere in the player source
    if match := DIRECT_M3U8.search(text):
        return unescape_js_string(match.group(0)).strip()

    # 2-4. Named-key regex strategies
    for pattern in (VALID_M3U8_ARRAY, VALID_M3U8, VALID_M3U8_ALT):
        if match := pattern.search(text):
            url = unescape_js_string(match.group(1)).strip()
            if ".m3u8" in url:
                return url

    return None


# --------------------------------------------------
async def process_event(url: str, url_num: int) -> tuple[str | None, str | None]:
    """Extract M3U8 stream URL (WITH token) and referer from an event page.

    Returns (m3u8_url, iframe_src) or (None, None) on failure.
    """
    nones = None, None

    if not (event_data := await network.request(url, url_num, log=log)):
        return nones

    if re.search(r"^https?://instreams?", url.lower()):
        # Direct iframe URL: page content is the iframe data itself
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
        # Strip any trailing junk characters that may have been captured
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
            # Normalize event timestamp field to "ts" for get_events()
            if "scheduled_at" in ev:
                ev["ts"] = ev.pop("scheduled_at")
            elif "timestamp" in ev:
                ev["ts"] = ev.pop("timestamp")
            ev["_sport"] = clean_sport_name(sport_name)
            ev["_sport_key"] = sport_key

        data.extend(items)

    if not data:
        return [{"timestamp": now_ts}]

    data[-1]["timestamp"] = now_ts
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

    start_dt = now.delta(hours=-48)
    end_dt = now.delta(hours=6)

    seen_events: set[str] = set()

    for ev in api_data:
        date = ev.get("ts") or ev.get("time")
        sport = ev.get("_sport")
        title = ev.get("title") or ev.get("name")
        home = ev.get("home_team")
        away = ev.get("away_team")

        if not (date and sport and title):
            continue

        # Build event name from title or home/away teams
        if home and away:
            event = get_event(home, away)
        else:
            event = title.strip()

        # Timestamp parsing
        try:
            if isinstance(date, (int, float)):
                event_dt = Time.from_ts(date)
            else:
                event_dt = Time.from_str(str(date), timezone="UTC")
        except Exception:
            continue

        if not start_dt <= event_dt <= end_dt:
            continue

        key = f"[{sport}] {event} ({TAG})"
        if key in cached_keys:
            continue

        event_id = str(ev.get("id") or f"{sport}_{title}_{str(date)[:10]}")
        if event_id in seen_events:
            continue
        seen_events.add(event_id)

        # Collect embed URLs from sources (label Server 1 = HOME, Server 2 = AWAY)
        sources = ev.get("sources") or []
        stream_urls: list[str] = []
        for src in sources:
            embed_url = src.get("embed_url")
            if not embed_url:
                continue
            if src.get("is_html_embed"):
                # HTML embeds aren't direct streams but can still be probed
                pass
            stream_urls.append(embed_url)

        if not stream_urls:
            continue

        events.append(
            {
                "sport": sport,
                "event": event,
                "link": stream_urls[0],
                "timestamp": event_dt.timestamp(),
                "stream_count": len(stream_urls),
                "all_streams": stream_urls,
            }
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

            # Fallback: try alternate streams (Server 2) if primary fails
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

            # Preserve referer from iframe; fallback to built referer
            referer = iframe_src or build_referer_from_stream(stream_url)

            key = f"[{ev['sport']}] {ev['event']} ({TAG})"
            tvg_id, logo = leagues.get_tvg_info(ev["sport"], ev["event"])

            # Keep the FULL token URL (do NOT strip ?st=...&e=...)
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

    # Clean old cache entries (older than 48 hours)
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
        # Keep full token URL — never split on '?st'
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
