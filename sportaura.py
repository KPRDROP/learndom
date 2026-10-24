import asyncio
import base64
import re
from collections.abc import KeysView
from dataclasses import dataclass
from functools import partial
from typing import Any
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import cloudscraper
from playwright.async_api import Browser, async_playwright

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

PLAYER_HOST = "frame.egybest.fit"

# Forced headers for the player request
PLAYER_REFERER = "https://frame.meth-streams-hd.cfd/"
PLAYER_ORIGIN = "https://frame.meth-streams-hd.cfd"


@dataclass(kw_only=True, slots=True)
class AURAEvent(Event):
    logo: str | None = None


def fix_league(s: str) -> str:
    splits = s.split()
    i = splits[0]
    return f"{i.upper() if len(i) <= 5 else i.capitalize()} {' '.join(x.capitalize() for x in splits[1:])}".strip()


def get_ifr(s: str) -> str | None:
    splits = urlsplit(s)
    s_id = splits.path.rstrip("/").split("/")[-1]

    if not s_id.isdigit():
        return None

    return urlunsplit(splits._replace(netloc=PLAYER_HOST, path=f"/frame/{s_id}"))


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


def _extract_source(raw: str) -> str | None:
    raw = raw.strip()
    if raw.startswith(("http://", "https://", "//")):
        return raw

    try:
        decoded = base64.b64decode(raw).decode("utf-8").strip()
        if decoded.startswith(("http://", "https://", "//")):
            return decoded
    except Exception:
        pass

    return raw or None


async def process_event(
    url: str,
    url_num: int,
    page: Any,
    iframe_url: str | None = None,
) -> tuple[str | None, str | None]:
    """Fetch the player page via a real Chromium page to pass Cloudflare."""
    nones = None, None

    ifr = get_ifr(iframe_url) if iframe_url else None
    if not ifr:
        ifr = get_ifr(url)

    if not ifr:
        log.warning(f"URL {url_num}) Unable to parse event id from {url}")
        return nones

    log.info(f"URL {url_num}) Player URL: {ifr}")

    try:
        resp = await page.goto(
            ifr,
            wait_until="domcontentloaded",
            timeout=15_000,
            referer=PLAYER_REFERER,
        )

        if not resp or not resp.ok:
            log.warning(
                f"URL {url_num}) Player status: "
                f"{resp.status if resp else 'None'}"
            )
            return nones

        html = await page.content()
        if "Just a moment" in html or "challenge-platform" in html:
            await page.wait_for_timeout(3_000)
            html = await page.content()

        pattern = re.compile(r'(activeStream|streamUrl)\s?=\s?"([^"]*)";', re.I)

        if not (match := pattern.search(html)):
            log.warning(f"URL {url_num}) No Clappr source found.")
            return nones

        source = _extract_source(match[2])

        if not source:
            log.warning(f"URL {url_num}) Empty source after decode")
            return nones

        log.info(f"URL {url_num}) Captured M3U8: {source[:80]}...")

        return source, ifr

    except Exception as e:
        log.warning(f"URL {url_num}) Playwright fetch failed: {e}")
        return nones


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


async def scrape(browser: Browser) -> None:
    cached_sources = CACHE_FILE.load()
    valid_sources = {k: v for k, v in cached_sources.items() if v.get("source")}
    valid_count = cached_count = len(valid_sources)

    urls.update(valid_sources)

    log.info(f"Loaded {cached_count} event(s) from cache")
    log.info(f'Scraping from "{BASE_URL}"')

    if events := await get_events(cached_sources.keys()):
        log.info(f"Processing {len(events)} new URL(s)")

        context = await browser.new_context(
            user_agent=VLC_UA,
            extra_http_headers={
                "Origin": PLAYER_ORIGIN,
                "Referer": PLAYER_REFERER,
            },
            viewport={"width": 1366, "height": 768},
            locale="en-US",
        )

        try:
            for i, ev in enumerate(events, start=1):
                page = await context.new_page()

                try:
                    handler = partial(
                        process_event,
                        url=ev.link,
                        url_num=i,
                        page=page,
                        iframe_url=ev.link,
                    )

                    source, iframe = await network.safe_process(
                        handler,
                        url_num=i,
                        timeout_return=(None, None),
                        semaphore=network.PW_S,
                        log=log,
                    )
                finally:
                    await page.close()

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

        finally:
            await context.close()

        log.info(f"Collected and cached {valid_count - cached_count} new event(s)")
    else:
        log.info("No new events found")

    CACHE_FILE.write(cached_sources)


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


async def main() -> None:
    async with async_playwright() as playwright:
        
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            await scrape(browser)
        finally:
            try:
                await browser.close()
            except Exception as e:
                log.warning(f"Error closing browser: {e}")

    write_outputs()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
