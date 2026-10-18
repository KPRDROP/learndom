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
}


def capture_req(req: Request, captured: list[str], got_one: asyncio.Event) -> None:
    if re.match(r"^https?://karimianian\.store", req.url):
        captured.append(req.url)
        got_one.set()


async def process_event(url: str, url_num: int, page: Page) -> str | None:
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
            log.error("URL %s) Status Code: %s", url_num, resp.status if resp else "None")
            return None

        wait_task = asyncio.create_task(got_one.wait())
        try:
            await asyncio.wait_for(wait_task, timeout=6)
        except TimeoutError:
            log.warning("URL %s) Timed out waiting for M3U8.", url_num)
            return None
        finally:
            if not wait_task.done():
                wait_task.cancel()
                try:
                    await wait_task
                except asyncio.CancelledError:
                    pass

        if captured:
            log.info("URL %s) Captured M3U8", url_num)
            return captured[0]
        return None
    except Exception as e:
        log.warning("URL %s) %s", url_num, e)
        return None
    finally:
        page.remove_listener("request", handler)


async def get_events(browser: Browser) -> list[Event]:
    now = Time.rn()
    events: list[Event] = []
    api_data: Any = None
    context = await browser.new_context(
        user_agent=DEFAULT_UA,
        extra_http_headers=BROWSER_HEADERS,
        viewport={"width": 1366, "height": 768},
        locale="en-US",
    )
    try:
        # A normal page visit is retained for the site's expected request flow.
        # This does not solve or bypass an access challenge imposed by the site.
        try:
            warm_page = await context.new_page()
            try:
                warm_resp = await warm_page.goto(
                    BASE_URL, wait_until="domcontentloaded", timeout=30_000
                )
                if warm_resp and warm_resp.status == 403:
                    log.warning("Site homepage also returned HTTP 403.")
            finally:
                await warm_page.close()
        except Exception as e:
            log.warning("Homepage request failed: %s", e)

        api_url = urljoin(BASE_URL, "?api")
        log.info('Requesting event API: "%s"', api_url)
        api_resp = await context.request.get(api_url, headers=BROWSER_HEADERS, timeout=30_000)

        if not api_resp.ok:
            try:
                body = await api_resp.text()
                snippet = re.sub(r"\s+", " ", body[:500])
            except Exception:
                snippet = "<response body unavailable>"

            if api_resp.status == 403:
                is_challenge = "just a moment" in snippet.lower() or "cloudflare" in snippet.lower()
                detail = "Cloudflare/security challenge" if is_challenge else "access denied"
                log.error(
                    "Event API returned HTTP 403 (%s). This scraper cannot bypass that "
                    "restriction. Ask the site/API owner for an authorized endpoint or "
                    "access method. Response: %s",
                    detail,
                    snippet,
                )
            else:
                log.error("Event API returned HTTP %s. Response: %s", api_resp.status, snippet)
            return events

        try:
            api_data = await api_resp.json()
        except Exception as e:
            log.error("Event API response was not valid JSON: %s", e)
            return events
    except Exception as e:
        log.error("Failed to fetch event API: %s", e)
        return events
    finally:
        try:
            await context.close()
        except Exception as e:
            log.warning("Error closing API context: %s", e)

    if not isinstance(api_data, dict):
        log.error("Event API JSON had an unexpected top-level type: %s", type(api_data).__name__)
        return events

    updated_date = api_data.get("updated")
    if not updated_date:
        log.warning('Event API payload is missing the "updated" field')
        return events
    try:
        updated_dt = Time.from_str(updated_date, tz_name="UTC")
    except Exception as e:
        log.warning("Could not parse updated date %r: %s", updated_date, e)
        return events
    if now.date() != updated_dt.date():
        log.info("Event API data is stale (updated %s); skipping", updated_date)
        return events

    raw_events = api_data.get("events", [])
    if not isinstance(raw_events, list):
        log.warning('Event API "events" field is not a list')
        return events

    for game in raw_events:
        if not isinstance(game, dict):
            continue
        values = [game.get(field) for field in ("title", "category", "url")]
        if not all(values):
            continue
        event_name, sport, url = values
        events.append(
            Event(sport=sport, name=event_name, link=url, timestamp=now.timestamp())
        )
    return events


async def scrape(browser: Browser) -> None:
    cached_sources = CACHE_FILE.load() or {}
    if isinstance(cached_sources, dict):
        urls.update(
            {key: value for key, value in cached_sources.items()
             if isinstance(value, dict) and value.get("source")}
        )
    else:
        cached_sources = {}

    if urls:
        log.info("Loaded %s event(s) from cache", len(urls))
        return

    log.info('Scraping from "%s"', BASE_URL)
    events = await get_events(browser)
    if events:
        log.info("Processing %s event URL(s)", len(events))
        async with network.event_context(browser) as context:
            for i, ev in enumerate(events, start=1):
                async with network.event_page(context) as page:
                    handler = partial(process_event, url=ev.link, url_num=i, page=page)
                    source = await network.safe_process(
                        handler, url_num=i, semaphore=network.PW_S, log=log
                    )
                    if not source:
                        continue
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
                    urls[key] = entry
        log.info("Collected %s event(s) with playable sources", len(urls))
    else:
        log.warning("No usable events were returned; retaining any valid cached playlist data")

    CACHE_FILE.write(cached_sources)


def _build_vlc_url(source: str, referer: str, ua: str) -> str:
    return f"{source}|referer={referer}|user-agent={ua}"


def _build_tivimate_url(source: str, referer: str, ua: str) -> str:
    return f"{source}|referer={referer}|user-agent={quote(ua, safe='')}"


def _sorted_entries() -> list[tuple[str, dict[str, Any]]]:
    return list(urls.items())


def _write_playlist(filename: str, tivimate: bool) -> None:
    entries = [
        (key, entry)
        for key, entry in _sorted_entries()
        if entry.get("source")
    ]
    if not entries:
        try:
            with open(filename, "r", encoding="utf-8"):
                log.warning("No valid event sources; leaving existing %s unchanged", filename)
                return
        except FileNotFoundError:
            log.warning("No valid event sources; not creating an empty playlist %s", filename)
            return

    try:
        with open(filename, "w", encoding="utf-8", newline="\n") as f:
            f.write("#EXTM3U\n")
            for chno, (key, entry) in enumerate(entries, start=1):
                source = entry["source"]
                referer = entry.get("refer") or DEFAULT_REFERER
                logo = entry.get("logo") or ""
                tvg_id = entry.get("tvg-id") or "Live.Event.us"
                name = key if tivimate else key.replace(f"({TAG})", "(GSPORTS)")
                f.write(
                    f'#EXTINF:-1 tvg-chno="{chno}" tvg-id="{tvg_id}" '
                    f'tvg-name="{name}" tvg-logo="{logo}" '
                    f'group-title="Live Events",{name}\n'
                )
                if not tivimate:
                    f.write(f"#EXTVLCOPT:http-referrer={referer}\n")
                    f.write(f"#EXTVLCOPT:http-user-agent={DEFAULT_UA}\n")
                    f.write(_build_vlc_url(source, referer, DEFAULT_UA) + "\n")
                else:
                    f.write(_build_tivimate_url(source, referer, DEFAULT_UA) + "\n")
        log.info("Wrote %s (%s event(s))", filename, len(entries))
    except Exception as e:
        log.error("Failed to write %s: %s", filename, e)


def write_vlc_m3u8(filename: str = "sportsg_vlc.m3u8") -> None:
    _write_playlist(filename, tivimate=False)


def write_tivimate_m3u8(filename: str = "sportsg.tivimate.m3u8") -> None:
    _write_playlist(filename, tivimate=True)


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
                log.warning("Error closing browser: %s", e)
    write_outputs()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
