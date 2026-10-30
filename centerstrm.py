from pathlib import Path
from urllib.parse import parse_qsl, urljoin, urlsplit
import os
import asyncio
import re
import httpx
from playwright.async_api import async_playwright
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

TAG = "STRMCNTR"

CACHE_FILE = Cache(f"{TAG.lower()}.json", exp=10_800)
OUTPUT_FILE = Path("centerstrm.m3u8")
OUTPUT_TIVI = Path("centerstrm_tivi.m3u8")

# API URL FROM SECRET
BASE_URL = os.environ["CENTERSTRM_API"]
ALT_BASE = os.environ["ALT_CENTERSTRM_API"]

UA_RAW = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/144.0.0.0 Safari/537.36"
)

REFERER = "https://embedcenter.st"
ORIGIN = "https://embedcenter.st"

UA_ENC = (
    "Mozilla%2F5.0%20(Windows%20NT%2010.0%3B%20Win64%3B%20x64)"
    "%20AppleWebKit%2F537.36%20(KHTML%2C%20like%20Gecko)"
    "%20Chrome%2F144.0.0.0%20Safari%2F537.36"
)

CATEGORIES = {
    4: "Basketball",
    9: "Football",
    #13: "Baseball",
    14: "American Football",
    15: "Motor Sport",
    16: "Hockey",
    17: "Fight MMA",
    18: "Boxing",
    19: "NCAA Sports",
    20: "WWE",
    21: "Tennis",
    22: "Soccer",
    23: "MMA",
    24: "Volleyball",
}

# Regex full m3u8
M3U8_FULL_RE = re.compile(
    r"https?://[^\s\"'<>]+\.m3u8[^\s\"'<>]*",
    re.IGNORECASE,
)


def normalize_stream_url(url: str | None) -> str | None:
    """Normalize captured m3u8 URL:
    - Replace escaped & (\\u0026, &amp;) with real &
    - Strip stray trailing separators like | or ,
    - Strip whitespace
    """
    if not url:
        return None

    u = url.strip()

    # Common escapes that show up in HTML/JS/JSON
    u = u.replace("\\u0026", "&")
    u = u.replace("\\u0026amp;", "&")
    u = u.replace("\\/", "/")
    u = u.replace("&amp;", "&")
    u = u.replace("&#38;", "&")
    u = u.replace("&#x26;", "&")
    u = u.replace("\\u0026", "&")

    # Sometimes trailing separators/pipes sneak in
    u = u.rstrip("|,; \t\r\n")

    # Drop any trailing pipe metadata if captured together
    if "|" in u:
        u = u.split("|", 1)[0].rstrip()

    return u


def cleanup(s: str) -> str:
    """Clean text by keeping ASCII characters from the last part after —"""
    return "".join(i for i in s.split("—")[-1] if i.isascii()).strip()


def fix_sport(s: str) -> str:
    """Fix sport name capitalization"""
    splits = s.split()
    if not splits:
        return s
    i = splits[0]
    return f"{i.upper() if len(i) < 5 else i.capitalize()} {' '.join(x.capitalize() for x in splits[1:])}".strip()


def find_full_m3u8_in_text(text: str) -> str | None:
    """Extract the first full m3u8 URL (with token) from any text"""
    if not text:
        return None
    matches = M3U8_FULL_RE.findall(text)
    # Prefer ones with token (?st=)
    for m in matches:
        normalized = normalize_stream_url(m)
        if normalized and ("?st=" in normalized or "?e=" in normalized):
            return normalized
    for m in matches:
        normalized = normalize_stream_url(m)
        if normalized:
            return normalized
    return None


async def extract_stream_from_page(page, url: str, url_num: int, captured: list) -> str | None:
    """Extract full m3u8 stream (with token) from page + network captures"""

    # 1) Check captured network responses for full m3u8 URL with token
    for u in captured:
        normalized = normalize_stream_url(u)
        if normalized and ".m3u8" in normalized and "?st=" in normalized:
            log.info(f"URL {url_num}) Captured FULL M3U8 from network")
            return normalized

    # 2) Check frames for m3u8 / stream param
    try:
        frames = page.frames
        for frame in frames:
            try:
                frame_url = frame.url
                if not frame_url or frame_url == "about:blank":
                    continue

                # Direct m3u8 in frame url
                if ".m3u8" in frame_url:
                    normalized = normalize_stream_url(frame_url)
                    if normalized and "?st=" in normalized:
                        log.info(f"URL {url_num}) Captured FULL M3U8 from frame URL")
                        return normalized

                # Stream ID in frame URL -> try to fetch full m3u8 from that frame
                splits = urlsplit(frame_url)
                params = dict(parse_qsl(splits.query))
                if stream_id := params.get("stream"):
                    # Try to get HTML of this frame and look for m3u8 with token
                    try:
                        frame_html = await frame.content()
                        if full := find_full_m3u8_in_text(frame_html):
                            log.info(f"URL {url_num}) Captured FULL M3U8 from frame HTML")
                            return full
                    except Exception:
                        pass

                    # Try to fetch the actual m3u8 URL (it may still be hidden)
                    candidate = normalize_stream_url(f"https://edgestream2.pro/hls/{stream_id}.m3u8")
                    if candidate:
                        captured.append(candidate)

            except Exception:
                continue
    except Exception:
        pass

    # 3) Fallback: parse main page HTML for m3u8 with token
    try:
        html = await page.content()
        if full := find_full_m3u8_in_text(html):
            log.info(f"URL {url_num}) Captured FULL M3U8 from page HTML")
            return full

        soup = HTMLParser(html)
        iframe = soup.css_first("iframe")
        if iframe and (src := iframe.attributes.get("src")):
            splits = urlsplit(src)
            params = dict(parse_qsl(splits.query))
            if stream_id := params.get("stream"):
                # Navigate to iframe source and inspect network
                try:
                    iframe_page = await page.context.new_page()
                    try:
                        # Capture m3u8 network requests from iframe page
                        iframe_captured = []
                        iframe_page.on(
                            "request",
                            lambda r: iframe_captured.append(r.url)
                            if ".m3u8" in r.url else None,
                        )
                        iframe_page.on(
                            "response",
                            lambda r: iframe_captured.append(r.url)
                            if ".m3u8" in r.url else None,
                        )

                        await iframe_page.goto(
                            src if src.startswith("http") else urljoin(url, src),
                            wait_until="domcontentloaded",
                            timeout=20000,
                        )
                        await iframe_page.wait_for_timeout(4000)

                        # Prefer ones with token
                        for u in iframe_captured:
                            normalized = normalize_stream_url(u)
                            if normalized and "?st=" in normalized:
                                log.info(f"URL {url_num}) Captured FULL M3U8 from iframe network")
                                return normalized

                        # Try HTML content
                        iframe_html = await iframe_page.content()
                        if full := find_full_m3u8_in_text(iframe_html):
                            if "?st=" in full:
                                log.info(f"URL {url_num}) Captured FULL M3U8 from iframe HTML")
                                return full

                        # Fallback: first captured (may still have no token)
                        if iframe_captured:
                            normalized = normalize_stream_url(iframe_captured[0])
                            log.warning(f"URL {url_num}) Using captured URL without token")
                            return normalized

                        # Last fallback: construct from stream ID
                        fallback = normalize_stream_url(f"https://edgestream2.pro/hls/{stream_id}.m3u8")
                        log.warning(f"URL {url_num}) Using constructed URL (no token)")
                        return fallback
                    finally:
                        try:
                            await iframe_page.close()
                        except Exception:
                            pass
                except Exception as e:
                    log.warning(f"URL {url_num}) Iframe fetch error: {str(e)[:60]}")
                    # Last fallback: constructed URL
                    fallback = normalize_stream_url(f"https://edgestream2.pro/hls/{stream_id}.m3u8")
                    log.warning(f"URL {url_num}) Using constructed URL (no token)")
                    return fallback
    except Exception as e:
        log.error(f"URL {url_num}) Extract error: {str(e)[:80]}")

    return None


async def process_event(url: str, url_num: int, context) -> str | None:
    """Process a single event using browser context to handle JS and capture full m3u8"""
    page = None
    captured = []
    try:
        page = await context.new_page()

        # Capture all m3u8 requests (with or without token)
        page.on(
            "request",
            lambda r: captured.append(r.url) if ".m3u8" in r.url else None,
        )
        page.on(
            "response",
            lambda r: captured.append(r.url) if ".m3u8" in r.url else None,
        )

        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # Give JS time to inject iframe and trigger m3u8 request
        await page.wait_for_timeout(5000)

        # Try to wait for iframe to appear
        try:
            await page.wait_for_selector("iframe", timeout=8000)
        except Exception:
            log.warning(f"URL {url_num}) No iframe appeared within timeout.")

        # Extra wait for network requests
        await page.wait_for_timeout(2000)

        stream = await extract_stream_from_page(page, url, url_num, captured)

        # If not yet full token, try one more wait for network
        if stream and "?st=" not in stream:
            log.info(f"URL {url_num}) Waiting for tokenized URL...")
            await page.wait_for_timeout(3000)
            for u in captured:
                normalized = normalize_stream_url(u)
                if normalized and ".m3u8" in normalized and "?st=" in normalized:
                    log.info(f"URL {url_num}) Captured FULL M3U8 after extra wait")
                    stream = normalized
                    break

        return normalize_stream_url(stream)

    except Exception as e:
        log.error(f"URL {url_num}) Browser error: {str(e)[:80]}")
        return None
    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass


async def get_events(cached_ids: set[str]) -> list[dict]:
    """Get events by parsing HTML from the website using browser"""
    now = Time.rn()
    events = []

    target_url = urljoin(BASE_URL, "game-cards/embed")
    log.info(f'Fetching events from "{target_url}"')

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        try:
            context = await browser.new_context(
                user_agent=UA_RAW,
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
            )

            page = await context.new_page()
            await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            html = await page.content()
            await page.close()

        finally:
            await browser.close()

    try:
        soup = HTMLParser(html)
    except Exception as e:
        log.error(f"Failed to parse HTML: {str(e)[:50]}")
        return events

    # Parse events from the page
    for card in soup.css(".game-card-group"):
        if not (sport_elem := card.css_first("h2")):
            continue

        sport = fix_sport(cleanup(sport_elem.text(strip=True)))

        for game in card.css(".game-card-row"):
            if not (event_time_elem := game.css_first(".game-card-when > time")):
                continue

            if not (event_time := event_time_elem.attributes.get("datetime")):
                continue

            try:
                event_dt = Time.fromisoformat(event_time).to_tz("EST")
            except Exception as e:
                log.debug(f"Failed to parse time: {str(e)[:30]}")
                continue

            if event_dt.date() != now.date():
                continue

            if not (name_elem := game.css_first("h3")):
                continue

            if not (event_name := name_elem.attributes.get("aria-label")):
                continue

            for source in game.css(".game-card-source > a.game-card-open-link"):
                if not (href := source.attributes.get("href")):
                    continue

                lang = source.text(strip=True)

                event_id = f"{sport}_{event_name}_{lang}".replace(" ", "_")

                if event_id in cached_ids:
                    continue

                if not href.startswith("http"):
                    href = urljoin(BASE_URL, href)

                events.append({
                    "id": event_id,
                    "sport": sport,
                    "event": f"{event_name} | {lang}" if lang else event_name,
                    "link": href,
                    "timestamp": now.timestamp(),
                    "language": lang,
                })

    log.info(f"Found {len(events)} events from HTML")
    return events


def build_playlist(data: dict) -> str:
    lines = ["#EXTM3U"]
    ch = 1

    for e in data.values():
        name = e["name"]
        url = normalize_stream_url(e["url"]) or e["url"]

        lines.append(
            f'#EXTINF:-1 tvg-chno="{ch}" '
            f'tvg-id="{e["id"]}" '
            f'tvg-name="{name}" '
            f'tvg-logo="{e["logo"]}" '
            f'group-title="Live Events",{name}'
        )

        lines.append(
            f'{url}'
            f'|referer={REFERER}/'
            f'|origin={ORIGIN}'
            f'|user-agent={UA_ENC}'
        )
        ch += 1

    return "\n".join(lines) + "\n"


def build_playlist_tivi(data: dict) -> str:
    """Build playlist with VLC-style headers for TiviMate"""
    lines = ["#EXTM3U"]
    ch = 1

    for e in data.values():
        name = e["name"]
        url = normalize_stream_url(e["url"]) or e["url"]

        lines.append(
            f'#EXTINF:-1 tvg-chno="{ch}" '
            f'tvg-id="{e["id"]}" '
            f'tvg-name="{name}" '
            f'tvg-logo="{e["logo"]}" '
            f'group-title="Live Events",{name}'
        )

        lines.append(f'#EXTVLCOPT:http-referrer={REFERER}/')
        lines.append(f'#EXTVLCOPT:http-origin={ORIGIN}')
        lines.append(f'#EXTVLCOPT:http-user-agent={UA_RAW}')
        lines.append(url)
        ch += 1

    return "\n".join(lines) + "\n"


async def scrape() -> None:
    cached = CACHE_FILE.load() or {}
    cached_ids = set(cached.keys())

    log.info(f"Loaded {len(cached)} cached events")

    # Normalize any escaped tokens already in cache
    for entry in cached.values():
        if "url" in entry:
            entry["url"] = normalize_stream_url(entry["url"]) or entry["url"]

    events = await get_events(cached_ids)
    log.info(f"Found {len(events)} live/upcoming events")

    if not events:
        OUTPUT_FILE.write_text(build_playlist(cached), encoding="utf-8")
        OUTPUT_TIVI.write_text(build_playlist_tivi(cached), encoding="utf-8")
        log.info(f"Wrote {len(cached)} entries to both playlists")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
                "--autoplay-policy=no-user-gesture-required",
            ],
        )

        try:
            context = await browser.new_context(
                user_agent=UA_RAW,
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )

            # Stealth: hide automation
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )

            processed = 0
            failed = 0

            for i, ev in enumerate(events, start=1):
                stream = await process_event(ev["link"], i, context)

                if not stream:
                    failed += 1
                    continue

                tvg_id, logo = leagues.get_tvg_info(ev["sport"], ev["event"])

                cache_key = ev["id"]

                cached[cache_key] = {
                    "name": f"[{ev['sport']}] {ev['event']} ({TAG})",
                    "url": normalize_stream_url(stream) or stream,
                    "logo": logo or "",
                    "timestamp": ev["timestamp"],
                    "id": tvg_id or "Live.Event.us",
                    "language": ev.get("language", ""),
                }
                processed += 1

            log.info(f"Successfully processed {processed} events, {failed} failed")

        finally:
            await browser.close()

    CACHE_FILE.write(cached)
    OUTPUT_FILE.write_text(build_playlist(cached), encoding="utf-8")
    OUTPUT_TIVI.write_text(build_playlist_tivi(cached), encoding="utf-8")

    log.info(f"Wrote {len(cached)} entries to centerstrm.m3u8 and centerstrm_tivi.m3u8")


if __name__ == "__main__":
    log.info("Starting StreamCenter updater...")
    asyncio.run(scrape())
