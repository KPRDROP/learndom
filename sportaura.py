import base64
import re
from collections.abc import KeysView
from dataclasses import dataclass
from functools import partial
from typing import Any
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import cloudscraper
from curl_cffi import requests as curl_requests
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "SPORTSA"

CACHE_FILE = Cache(TAG, exp=10_800)

API_FILE = Cache(f"{TAG}-api", exp=19_800)

BASE_URL = "https://aurasports.xyz"

API_URL = "https://aurasports.xyz/api/schedule"

TIVIMATE_UA = (
    "Mozilla%2F5.0%20(Windows%20NT%2010.0%3B%20Win64%3B%20x64)%20"
    "AppleWebKit%2F537.36%20(KHTML%2C%20like%20Gecko)%20"
    "Chrome%2F111.0.0.0%20Safari%2F537.36"
)

VLC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/111.0.0.0 Safari/537.36"
)

DEFAULT_REFERER = BASE_URL

EVENT_WINDOW_MINUTES = 180

# Player host
PLAYER_HOST = "frame.egybest.fit"


@dataclass(kw_only=True, slots=True)
class AURAEvent(Event):
    logo: str | None = None


def fix_league(s: str) -> str:
    splits = s.split()
    i = splits[0]
    return f"{i.upper() if len(i) <= 5 else i.capitalize()} {' '.join(x.capitalize() for x in splits[1:])}".strip()


def get_ifr(s: str) -> str | None:
    """Map any event/iframe URL to https://<PLAYER_HOST>/frame/<id>.

    Mirrors the original working code's get_ifr() exactly.
    """
    splits = urlsplit(s)
    s_id = splits.path.rstrip("/").split("/")[-1]

    return (
        urlunsplit(splits._replace(netloc=PLAYER_HOST, path=f"/frame/{s_id}"))
        if s_id.isdigit()
        else None
    )


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _get_cloudscraper() -> cloudscraper.CloudScraper:
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False},
        interpreter="js2py",
        delay=10,
    )
    scraper.headers.update({
        "User-Agent": VLC_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": DEFAULT_REFERER,
    })
    return scraper


def _fetch_api_with_cloudscraper() -> list[dict[str, Any]] | None:
    scraper = _get_cloudscraper()
    scraper.headers["Accept"] = "application/json, text/plain, */*"

    log.info(f'Fetching API via cloudscraper: "{API_URL}"')

    try:
        resp = scraper.get(API_URL, timeout=30)
    except Exception as e:
        log.error(f"cloudscraper request failed: {e}")
        return None

    if resp.status_code != 200:
        log.error(f"API status {resp.status_code}: {resp.text[:200]}")
        return None

    if "Just a moment" in resp.text:
        log.error("cloudscraper received Cloudflare challenge page.")
        return None

    try:
        data = resp.json()
    except Exception as e:
        log.error(f"API response not JSON: {e}")
        return None

    if not isinstance(data, list):
        log.error(f"Unexpected API response type: {type(data)}")
        return None

    return data


def _fetch_html(url: str, referer: str = DEFAULT_REFERER) -> str | None:
    """Fetch an HTML page — tries curl_cffi first (Chrome TLS fingerprint),
    then falls back to cloudscraper."""
    try:
        resp = curl_requests.get(
            url,
            impersonate="chrome",
            headers={
                "Referer": referer,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=30,
            allow_redirects=True,
        )
        if resp.status_code == 200 and "Just a moment" not in resp.text:
            return resp.text
        log.debug(f"curl_cffi status {resp.status_code} for {url} — falling back")
    except Exception as e:
        log.debug(f"curl_cffi failed for {url}: {e} — falling back")

    # Fallback
    scraper = _get_cloudscraper()
    scraper.headers["Referer"] = referer
    try:
        resp = scraper.get(url, timeout=30)
        if resp.status_code == 200 and "Just a moment" not in resp.text:
            return resp.text
        log.warning(f"Both fetchers failed for {url} (status {resp.status_code})")
    except Exception as e:
        log.warning(f"cloudscraper failed for {url}: {e}")

    return None


async def process_event(
    url: str,
    url_num: int,
    iframe_url: str | None = None,
) -> tuple[str | None, str | None]:
    """Extract the M3U8 stream URL.

    The original working code doesn't parse the event page for an <iframe>.
    It derives the player URL directly from the numeric ID in the event URL
    (or the API's iframeUrl) and reads `activeStream` / `streamUrl` from there.
    """
    nones = None, None

    # 1. Derive the player URL from the event url or the API-provided iframeUrl.
    ifr = get_ifr(iframe_url) if iframe_url else None
    if not ifr:
        ifr = get_ifr(url)

    if not ifr:
        log.warning(f"URL {url_num}) Unable to parse event id from {url}")
        return nones

    log.info(f"URL {url_num}) Player URL: {ifr}")

    # 2. Fetch the player page (contains `activeStream` / `streamUrl`).
    event_html = _fetch_html(ifr, referer=url)
    if not event_html:
        log.warning(f"URL {url_num}) Failed to fetch player page")
        return nones

    # 3. Match either variable name (original code supports both).
    pattern = re.compile(r'(activeStream|streamUrl)\s?=\s?"([^"]*)";', re.I)

    if not (match := pattern.search(event_html)):
        log.warning(f"URL {url_num}) No Clappr source found.")
        log.debug(f"URL {url_num}) Player HTML snippet: {event_html[:500]}")
        return nones

    raw = match[2]

source: str | None = None
    if raw.startswith(("http://", "https://", "//")):
        source = raw.strip()
    else:
        try:
            decoded = base64.b64decode(raw).decode("utf-8").strip()
            if decoded.startswith(("http://", "https://", "//")):
                source = decoded
        except Exception:
            pass
        if source is None:
            source = raw.strip()

    if not source:
        log.warning(f"URL {url_num}) Empty source after decode")
        return nones

    log.info(f"URL {url_num}) Captured M3U8: {source[:80]}...")

    return source, ifr


async def get_events(cached_keys: KeysView[str]) -> list[AURAEvent]:
    now = Time.rn()
    events: list[AURAEvent] = []

    api_data = API_FILE.load(per_entry=False, ts_index=-1)

    if not api_data:
        log.info("Refreshing API cache")
        api_data = _fetch_api_with_cloudscraper()

        if api_data:
            api_data.append({"timestamp": now.timestamp()})
            API_FILE.write(api_data)

    if not api_data:
        log.warning("No API data available")
        return events

    start_dt = now.delta(minutes=-EVENT_WINDOW_MINUTES)
    end_dt = now.delta(minutes=EVENT_WINDOW_MINUTES)

    for category in api_data:
        if not isinstance(category, dict):
            continue

        for event in category.get("schedule", []):
            if not all(
                values := [
                    event.get(x)
                    for x in ("title", "category", "date", "iframeUrl")
                ]
            ):
                continue

            name, sport, start_ts, link = values

            sport = (
                league_name
                if (league_name := event.get("league_name"))
                else fix_league(sport)
            )

            event_dt = Time.from_ts(event_ts := int(f"{start_ts}"[:-3]))

            if not start_dt <= event_dt <= end_dt:
                continue

            elif f"[{sport}] {name} ({TAG})" in cached_keys:
                continue

            events.append(
                AURAEvent(
                    sport=sport,
                    name=name,
                    logo=event.get("thumbnail"),
                    link=link,
                    timestamp=event_ts,
                )
            )

    return events


async def scrape() -> None:
    cached_sources = CACHE_FILE.load()
    valid_sources = {k: v for k, v in cached_sources.items() if v.get("source")}
    valid_count = cached_count = len(valid_sources)

    urls.update(valid_sources)

    log.info(f"Loaded {cached_count} event(s) from cache")
    log.info(f'Scraping from "{BASE_URL}"')

    if events := await get_events(cached_sources.keys()):
        log.info(f"Processing {len(events)} new URL(s)")

        for i, ev in enumerate(events, start=1):
            handler = partial(
                process_event,
                url=ev.link,
                url_num=i,
                iframe_url=ev.link,
            )

            source, iframe = await network.safe_process(
                handler,
                url_num=i,
                timeout_return=(None, None),
                semaphore=network.HTTP_S,
                log=log,
            )

            key = f"[{ev.sport}] {ev.name} ({TAG})"
            tvg_id, logo = leagues.get_tvg_info(ev.sport, ev.name)

            entry = {
                "source": source,
                "logo": ev.logo or logo,
                "refer": iframe,
                "timestamp": ev.timestamp,
                "tvg-id": tvg_id or "Live.Event.us",
                "link": ev.link,
            }

            cached_sources[key] = entry

            if source:
                valid_count += 1
                urls[key] = entry

        log.info(f"Collected and cached {valid_count - cached_count} new event(s)")
    else:
        log.info("No new events found")

    CACHE_FILE.write(cached_sources)


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------
def _build_vlc_url(source: str, referer: str, ua: str) -> str:
    return f"{source}|referer={referer}|user-agent={ua}"


def _build_tivimate_url(source: str, referer: str, encoded_ua: str) -> str:
    return f"{source}|referer={referer}|user-agent={encoded_ua}"


def _sorted_entries() -> list[tuple[str, dict[str, Any]]]:
    return list(urls.items())


def write_vlc_m3u8(filename: str = "sportaura_vlc.m3u8") -> None:
    try:
        with open(filename, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            for chno, (key, entry) in enumerate(_sorted_entries(), start=1):
                source = entry.get("source")
                if not source:
                    continue
                referer = entry.get("refer") or DEFAULT_REFERER
                logo = entry.get("logo") or ""
                tvg_id = entry.get("tvg-id") or "Live.Event.us"
                f.write(
                    f'#EXTINF:-1 tvg-chno="{chno}" tvg-id="{tvg_id}" '
                    f'tvg-name="{key}" tvg-logo="{logo}" '
                    f'group-title="Live Events",{key}\n'
                )
                f.write(f"#EXTVLCOPT:http-referrer={referer}\n")
                f.write(f"#EXTVLCOPT:http-user-agent={VLC_UA}\n")
                f.write(_build_vlc_url(source, referer, VLC_UA) + "\n")
        log.info(f"Wrote {filename} ({len(urls)} event(s))")
    except Exception as e:
        log.error(f"Failed to write {filename}: {e}")


def write_tivimate_m3u8(filename: str = "sportaura_tivimate.m3u8") -> None:
    try:
        with open(filename, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            for chno, (key, entry) in enumerate(_sorted_entries(), start=1):
                source = entry.get("source")
                if not source:
                    continue
                referer = entry.get("refer") or DEFAULT_REFERER
                logo = entry.get("logo") or ""
                tvg_id = entry.get("tvg-id") or "Live.Event.us"
                f.write(
                    f'#EXTINF:-1 tvg-chno="{chno}" tvg-id="{tvg_id}" '
                    f'tvg-name="{key}" tvg-logo="{logo}" '
                    f'group-title="Live Events",{key}\n'
                )
                f.write(_build_tivimate_url(source, referer, TIVIMATE_UA) + "\n")
        log.info(f"Wrote {filename} ({len(urls)} event(s))")
    except Exception as e:
        log.error(f"Failed to write {filename}: {e}")


def write_outputs() -> None:
    write_vlc_m3u8()
    write_tivimate_m3u8()


# ---------------------------------------------------------------------------
# Entrypoints
# ---------------------------------------------------------------------------
async def main() -> None:
    await scrape()
    write_outputs()


def run() -> None:
    import asyncio
    asyncio.run(main())


if __name__ == "__main__":
    run()
