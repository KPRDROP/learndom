import asyncio
import json
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

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/155.0.0.0 Safari/537.36"
)

DEFAULT_REFERER = BASE_URL

BROWSER_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": DEFAULT_REFERER,
    "Origin": DEFAULT_REFERER.rstrip("/"),
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Upgrade-Insecure-Requests": "1",
}


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
    handler = partial(capture_req, captured=captured, got_one=got_one)
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


async def get_events(browser: Browser) -> list[Event]:
    """
    Fetch events via the site's API. Uses a real browser context that must
    first clear Cloudflare's managed challenge on the main page.
    """
    now = Time.rn()
    events: list[Event] = []

    context = await browser.new_context(
        user_agent=DEFAULT_UA,
        extra_http_headers=BROWSER_HEADERS,
        viewport={"width": 1366, "height": 768},
        locale="en-US",
    )

    try:
        # --- Step 1: open the main page and actually wait for Cloudflare.
        page = await context.new_page()
        try:
            await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:
            log.warning(f"Initial navigation failed: {e}")
            return events

        # Cloudflare's interstitial clears asynchronously. Poll until the
        # challenge markers disappear (or timeout). The page title changes
        # from "Just a moment..." to the real site title.
        cleared = False
        for _ in range(30):  # up to ~30 seconds
            title = (await page.title()).lower()
            body = await page.content()

            if "just a moment" not in title and "challenge-platform" not in body:
                cleared = True
                break
            await page.wait_for_timeout(1_000)

        if not cleared:
            log.error("Cloudflare challenge on main page did not clear in time.")
            return events

        log.info("Main page cleared Cloudflare challenge.")

        # --- Step 2: fetch the API *inside the same context* so the
        # cf_clearance cookie travels with the request.
        api_url = urljoin(BASE_URL, "?api")
        log.info(f'Fetching API: "{api_url}"')

        api_resp = await context.request.get(
            api_url,
            headers=BROWSER_HEADERS,
            timeout=30_000,
        )

        if not api_resp.ok:
            snippet = (await api_resp.text())[:200].replace("\n", " ")
            log.error(f"API status {api_resp.status}: {snippet}")
            return events

        raw = await api_resp.text()

        # Guard: if we still got the HTML challenge, bail out cleanly.
        if raw.lstrip().startswith("<!DOCTYPE") or "Just a moment" in raw:
            log.error("API returned the Cloudflare challenge page instead of JSON.")
            return events

        try:
            api_data: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError as e:
            log.error(f"API response was not valid JSON: {e}")
            return events

    except Exception as e:
        log.error(f"Failed to fetch API: {e}")
        return events
    finally:
        try:
            await context.close()
        except Exception:
            pass

    # --- Parse events from the JSON payload.
    if not (updated_date := api_data.get("updated")):
        log.warning('API payload missing "updated" field')
        return events

    try:
        updated_dt = Time.from_str(updated_date, tz_name="UTC")
    except Exception as e:
        log.warning(f"Could not parse updated date {updated_date!r}: {e}")
        return events

    if now.date() != updated_dt.date():
        log.info(f"API data is stale (updated {updated_date}); skipping")
        return events

    for game in api_data.get("events", []):
        title = game.get("title")
        category = game.get("category")
        url = game.get("url")

        if not (title and category and url):
            continue

        events.append(
            Event(
                sport=category,
                name=title,
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

    if events := await get_events(browser):
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


def _build_vlc_url(source: str, referer: str, ua: str) -> str:
    return f"{source}|referer={referer}|user-agent={ua}"


def _build_tivimate_url(source: str, referer: str, ua: str) -> str:
    encoded_ua = quote(ua, safe="")
    return f"{source}|referer={referer}|user-agent={encoded_ua}"


def _sorted_entries() -> list[tuple[str, dict[str, Any]]]:
    return list(urls.items())


def write_vlc_m3u8(filename: str = "sportsg_vlc.m3u8") -> None:
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
                vlc_key = key.replace(f"({TAG})", "(GSPORTS)")

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
    write_vlc_m3u8()
    write_tivimate_m3u8()


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
