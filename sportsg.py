import asyncio
import re
from functools import partial
from typing import Any
from urllib.parse import quote, urljoin

from playwright.async_api import Browser, Page, Request, async_playwright

from utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "SPORTSG"

CACHE_FILE = Cache(TAG, exp=19_800)

BASE_URL = "https://gsports.lat/"

# Default User-Agent used for both VLC EXTVLCOPT and TiviMate headers
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/155.0.0.0 Safari/537.36"
)

# Referer default (fallback if event link is missing)
DEFAULT_REFERER = BASE_URL


# ---------------------------------------------------------------------------
# Request capture
# ---------------------------------------------------------------------------
def capture_req(
    req: Request,
    captured: list[str],
    got_one: asyncio.Event,
) -> None:

    pattern = re.compile(r"^https?://karimianian.store")

    if pattern.search(req.url):
        captured.append(req.url)
        got_one.set()


async def process_event(
    url: str,
    url_num: int,
    page: Page,
) -> str | None:

    captured: list[str] = []

    got_one = asyncio.Event()

    handler = partial(
        capture_req,
        captured=captured,
        got_one=got_one,
    )

    page.on("request", handler)

    try:
        resp = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=6_000,
            referer=BASE_URL,
        )

        if not resp or not resp.ok:
            log.error(f"URL {url_num}) Status Code: {resp.status if resp else 'None'}")
            return

        wait_task = asyncio.create_task(got_one.wait())

        try:
            await asyncio.wait_for(wait_task, timeout=6)
        except TimeoutError:
            log.warning(f"URL {url_num}) Timed out waiting for M3U8.")
            return

        finally:
            if not wait_task.done():
                wait_task.cancel()

                try:
                    await wait_task
                except asyncio.CancelledError:
                    pass

        if captured:
            log.info(f"URL {url_num}) Captured M3U8")
            return captured[0]

    except Exception as e:
        log.warning(f"URL {url_num}) {e}")
        return

    finally:
        page.remove_listener("request", handler)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
async def get_events() -> list[Event]:
    now = Time.rn()

    events: list[Event] = []

    if not (api_req := await network.request(urljoin(BASE_URL, "?api"), log=log)):
        return events

    api_data: dict[str, Any] = api_req.json()

    if not (updated_date := api_data.get("updated")):
        return events

    updated_dt = Time.from_str(updated_date, tz_name="UTC")

    if now.date() != updated_dt.date():
        return events

    for game in api_data.get("events", []):
        if not all(
            values := [
                game.get(x)
                for x in (
                    "title",
                    "category",
                    "url",
                )
            ]
        ):
            continue

        event_name, sport, url = values

        events.append(
            Event(
                sport=sport,
                name=event_name,
                link=url,
                timestamp=now.timestamp(),
            )
        )

    return events


async def scrape(browser: Browser) -> None:
    cached_sources = CACHE_FILE.load() or {}

    if cached_sources:
        urls.update({k: v for k, v in cached_sources.items() if v.get("source")})

        log.info(f"Loaded {len(urls)} event(s) from cache")

        return

    log.info(f'Scraping from "{BASE_URL}"')

    if events := await get_events():
        log.info(f"Processing {len(events)} new URL(s)")

        async with network.event_context(browser) as context:
            for i, ev in enumerate(events, start=1):
                async with network.event_page(context) as page:
                    handler = partial(
                        process_event,
                        url=ev.link,
                        url_num=i,
                        page=page,
                    )

                    source = await network.safe_process(
                        handler,
                        url_num=i,
                        semaphore=network.PW_S,
                        log=log,
                    )

                    key = f"[{ev.sport}] {ev.name} ({TAG})"

                    tvg_id, logo = leagues.get_tvg_info(ev.sport, ev.name)

                    entry = {
                        "source": source,
                        "logo": logo,
                        "refer": ev.link,
                        "timestamp": ev.timestamp,
                        "tvg-id": tvg_id or "Live.Event.us",
                    }

                    cached_sources[key] = entry

                    if source:
                        urls[key] = entry

        log.info(f"Collected and cached {len(urls)} event(s)")

    else:
        log.info("No new events found")

    CACHE_FILE.write(cached_sources)


# ---------------------------------------------------------------------------
# Helpers to build the stream / URL pieces
# ---------------------------------------------------------------------------
def _build_vlc_url(source: str, referer: str, ua: str) -> str:
    """Build the URL with VLC-style pipe headers (unencoded UA)."""
    return f"{source}|referer={referer}|user-agent={ua}"


def _build_tivimate_url(source: str, referer: str, ua: str) -> str:
    """Build the URL with TiviMate-style pipe headers (URL-encoded UA)."""
    encoded_ua = quote(ua, safe="")
    return f"{source}|referer={referer}|user-agent={encoded_ua}"


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------
def _sorted_entries() -> list[tuple[str, dict[str, Any]]]:
    """Stable order: sort by event name (strip tag suffix) then insertion order."""
    return list(urls.items())


def write_vlc_m3u8(filename: str = "sportsg_vlc.m3u8") -> None:
    """Write sportsg_vlc.m3u8 (EXTVLCOPT headers, raw UA)."""
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

                # human-readable name used in EXTINF
                name = key  # already in form: "[Sport] Event (SPORTSG)"
                vlc_key = name.replace(f"({TAG})", "(GSPORTS)")

                f.write(
                    f'#EXTINF:-1 tvg-chno="{chno}" tvg-id="{tvg_id}" '
                    f'tvg-name="{vlc_key}" tvg-logo="{logo}" '
                    f'group-title="Live Events",{vlc_key}\n'
                )
                f.write(f"#EXTVLCOPT:http-referrer={referer}\n")
                f.write(f"#EXTVLCOPT:http-user-agent={DEFAULT_UA}\n")
                f.write(_build_vlc_url(source, referer, DEFAULT_UA) + "\n")

        log.info(f"Wrote {filename} ({len(urls)} event(s))")
    except Exception as e:
        log.error(f"Failed to write {filename}: {e}")


def write_tivimate_m3u8(filename: str = "sportsg.tivimate.m3u8") -> None:
    """Write sportsg.tivimate.m3u8 (pipe headers, encoded UA)."""
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

                # TiviMate file uses SPORTSG tag (as in the key)
                name = key

                f.write(
                    f'#EXTINF:-1 tvg-chno="{chno}" tvg-id="{tvg_id}" '
                    f'tvg-name="{name}" tvg-logo="{logo}" '
                    f'group-title="Live Events",{name}\n'
                )
                f.write(_build_tivimate_url(source, referer, DEFAULT_UA) + "\n")

        log.info(f"Wrote {filename} ({len(urls)} event(s))")
    except Exception as e:
        log.error(f"Failed to write {filename}: {e}")


def write_outputs() -> None:
    """Generate both output files (VLC + TiviMate)."""
    write_vlc_m3u8()
    write_tivimate_m3u8()


# ---------------------------------------------------------------------------
# Entrypoints
# ---------------------------------------------------------------------------
async def main() -> None:
    async with async_playwright() as playwright:
        browser = await network.browser(playwright)
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
