import asyncio
import re
from functools import partial
from urllib.parse import urljoin
from datetime import datetime

from playwright.async_api import Browser, Page, TimeoutError, async_playwright
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "SPFIT"

CACHE_FILE = Cache(TAG, exp=28_800)

BASE_URL = "https://streamseast.eu"

# Output files
VLC_OUTPUT = "spfit_vlc.m3u8"
TIVIMATE_OUTPUT = "spfit_tivimate.m3u8"

# Headers
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
USER_AGENT_ENCODED = "Mozilla%2F5.0%20(Windows%20NT%2010.0%3B%20Win64%3B%20x64)%20AppleWebKit%2F537.36%20(KHTML%2C%20like%20Gecko)%20Chrome%2F120.0.0.0%20Safari%2F537.36"
REFERER = "https://edgesport.cfd/"
ORIGIN = "https://edgesport.cfd"

# Sport categories with URL mapping (matching original sportspass.py structure)
SPORT_URLS = {
    sport: urljoin(BASE_URL, sport.lower())
    for sport in [
        "Soccer",
        "NBA",
        "NFL",
        "MLB",
        "NHL",
        "MMA",
        "Boxing",
        "F1",
    ]
}


async def process_event(
    url: str,
    url_num: int,
    page: Page,
) -> tuple[str | None, str | None, str | None]:

    nones = None, None, None

    captured: list[str] = []

    got_one = asyncio.Event()

    handler = partial(
        network.capture_req,
        captured=captured,
        got_one=got_one,
    )

    page.on("request", handler)

    event_name = "Sporting Event"

    try:
        resp = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=10_000,
        )

        if not resp or resp.status != 200:
            log.error(f"URL {url_num}) Status Code: {resp.status if resp else 'None'}")
            return (event_name, *nones)

        event_name_elem = page.locator("h1.match-head")

        try:
            event_name = await event_name_elem.inner_text(timeout=2_500)
        except TimeoutError:
            log.warning(f"URL {url_num}) Could not get event name, using default")
            event_name = f"Event {url_num}"

        # Clean event name
        event_name = clean_event_name(event_name)

        try:
            ifr = page.locator("iframe.embed-responsive-item")
            await ifr.wait_for(timeout=2_500)
            ifr_src = await ifr.get_attribute("src")
        except TimeoutError:
            log.warning(f"URL {url_num}) No iframe found.")
            return (event_name, *nones)

        if not ifr_src:
            log.warning(f"URL {url_num}) Empty iframe src.")
            return (event_name, *nones)

        await page.goto(
            ifr_src,
            wait_until="domcontentloaded",
            timeout=5_000,
        )

        wait_task = asyncio.create_task(got_one.wait())

        try:
            await asyncio.wait_for(wait_task, timeout=10)
        except TimeoutError:
            log.warning(f"URL {url_num}) Timed out waiting for M3U8.")
            return (event_name, ifr_src, None)

        finally:
            if not wait_task.done():
                wait_task.cancel()
                try:
                    await wait_task
                except asyncio.CancelledError:
                    pass

        if captured:
            # Filter out indianservers links
            valid_streams = [c for c in captured if "indianservers" not in c.lower()]
            
            if not valid_streams:
                log.warning(f"URL {url_num}) Unsuitable M3U8 link captured.")
                return (event_name, ifr_src, None)

            log.info(f"URL {url_num}) Captured M3U8: {event_name[:50]}")
            return event_name, ifr_src, valid_streams[0]

        return (event_name, ifr_src, None)

    except Exception as e:
        log.warning(f"URL {url_num}) Error: {str(e)[:100]}")
        return (event_name, *nones)

    finally:
        page.remove_listener("request", handler)


def clean_event_name(event_name: str) -> str:
    """Clean event name by removing commas and extra spaces"""
    if not event_name:
        return "Sporting Event"
    
    cleaned = event_name.replace(",", "")
    cleaned = re.sub(r'\s+', ' ', cleaned)
    cleaned = re.sub(r'\s*-\s*(?:Live|Stream|Watch|SPFIT)\s*$', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*\|.*$', '', cleaned)  # Remove anything after pipe
    
    return cleaned.strip() or "Sporting Event"


async def get_events(cached_links: set[str]) -> list[Event]:
    now = Time.rn()

    tasks = [network.request(url, log=log) for url in SPORT_URLS.values()]

    results = await asyncio.gather(*tasks)

    events: list[Event] = []

    if not (
        soups := [(HTMLParser(html.content), html.url) for html in results if html]
    ):
        return events

    # Expanded time window to capture more events
    start_dt = now.delta(hours=-6)
    end_dt = now.delta(hours=12)

    date_ptrn = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z", re.I)

    for soup, url in soups:
        sport = next((k for k, v in SPORT_URLS.items() if v == url), "Live Event")

        for event in soup.css("a.matches"):
            if not (href := event.attributes.get("href")):
                continue

            elif (link := urljoin(BASE_URL, href)) in cached_links:
                continue

            if (scr_elem := event.css_first("script")) and (
                match := date_ptrn.search(scr_elem.text(strip=True))
            ):
                event_dt = Time.fromisoformat(match[0]).to_tz("EST")

            elif event.css_first("span.status-badge.badge.bg-success"):
                event_dt = now

            else:
                # Try to get event name from text if no date
                event_text = event.text(strip=True)
                if event_text and len(event_text) > 3:
                    # Include events without dates if they look valid
                    event_dt = now
                else:
                    continue

            if not start_dt <= event_dt <= end_dt:
                continue

            events.append(
                Event(
                    sport=sport,
                    link=link,
                    timestamp=event_dt.timestamp(),
                )
            )

    # Remove duplicates
    seen_links = set()
    unique_events = []
    for ev in events:
        if ev.link not in seen_links:
            seen_links.add(ev.link)
            unique_events.append(ev)

    log.info(f"Found {len(unique_events)} unique events (after deduplication)")
    return unique_events


async def scrape(browser: Browser) -> None:
    cached_urls = CACHE_FILE.load()

    cached_links = {entry["link"] for entry in cached_urls.values() if entry.get("link")}

    valid_urls = {k: v for k, v in cached_urls.items() if v.get("source")}

    valid_count = cached_count = len(valid_urls)

    urls.update(valid_urls)

    log.info(f"Loaded {cached_count} event(s) from cache")

    log.info(f'Scraping from "{BASE_URL}"')

    if events := await get_events(cached_links):
        log.info(f"Processing {len(events)} URL(s)")

        async with network.event_context(browser) as context:
            for i, ev in enumerate(events, start=1):
                async with network.event_page(context) as page:
                    handler = partial(
                        process_event,
                        url=ev.link,
                        url_num=i,
                        page=page,
                    )

                    name, ifr_src, source = await network.safe_process(
                        handler,
                        url_num=i,
                        timeout_return=(None, None, None),
                        semaphore=network.PW_S,
                        log=log,
                    )

                    if not name:
                        name = f"Event {i}"

                    tvg_id, logo = leagues.get_tvg_info(ev.sport, name)

                    key = f"[{ev.sport}] {name} ({TAG})"

                    entry = {
                        "source": source,
                        "logo": logo,
                        "refer": ifr_src,
                        "timestamp": ev.timestamp,
                        "tvg-id": tvg_id or "Live.Event.us",
                        "link": ev.link,
                    }

                    cached_urls[key] = entry

                    if source:
                        valid_count += 1
                        urls[key] = entry

        log.info(f"Collected and cached {valid_count - cached_count} new event(s)")

    else:
        log.info("No new events found")

    CACHE_FILE.write(cached_urls)


def generate_playlists() -> None:
    """Generate VLC and TiviMate playlist files from collected events"""
    if not urls:
        log.warning("No events to generate playlists")
        with open(VLC_OUTPUT, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n# No events available\n")
        with open(TIVIMATE_OUTPUT, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n# No events available\n")
        return

    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    header = f'#EXTM3U x-tvg-url="https://epgshare01.online/epgshare01/epg_ripper_ALL_SOURCES1.xml.gz"\n# Last Updated: {ts}\n# Total Streams: {len(urls)}\n\n'

    # Generate VLC playlist
    try:
        with open(VLC_OUTPUT, "w", encoding="utf-8") as f:
            f.write(header)

            ch_no = 1
            for event_name, event_data in urls.items():
                url = event_data.get("source")
                logo = event_data.get("logo", "https://i.gyazo.com/1c4aa937f5ea01b0f29bb27adb59884c.png")
                tvg_id = event_data.get("tvg-id", "Live.Event.us")

                if not url:
                    continue

                clean_name = clean_event_name(event_name)

                f.write(
                    f'#EXTINF:-1 tvg-chno="{ch_no}" tvg-id="{tvg_id}" tvg-name="{clean_name}" '
                    f'tvg-logo="{logo}" group-title="Live Events",{clean_name}\n'
                )
                f.write(f'#EXTVLCOPT:http-referrer={REFERER}\n')
                f.write(f'#EXTVLCOPT:http-origin={ORIGIN}\n')
                f.write(f'#EXTVLCOPT:http-user-agent={USER_AGENT}\n')
                f.write(f'{url}\n\n')

                ch_no += 1

        log.info(f"Generated VLC playlist: {VLC_OUTPUT} with {ch_no - 1} streams")
    except Exception as e:
        log.error(f"Error generating VLC playlist: {e}")

    # Generate TiviMate playlist
    try:
        with open(TIVIMATE_OUTPUT, "w", encoding="utf-8") as f:
            f.write(header)

            ch_no = 1
            for event_name, event_data in urls.items():
                url = event_data.get("source")
                logo = event_data.get("logo", "https://i.gyazo.com/1c4aa937f5ea01b0f29bb27adb59884c.png")
                tvg_id = event_data.get("tvg-id", "Live.Event.us")

                if not url:
                    continue

                clean_name = clean_event_name(event_name)

                f.write(
                    f'#EXTINF:-1 tvg-chno="{ch_no}" tvg-id="{tvg_id}" tvg-name="{clean_name}" '
                    f'tvg-logo="{logo}" group-title="Live Events",{clean_name}\n'
                )
                f.write(f'{url}|referer={REFERER}|origin={ORIGIN}|user-agent={USER_AGENT_ENCODED}\n\n')

                ch_no += 1

        log.info(f"Generated TiviMate playlist: {TIVIMATE_OUTPUT} with {ch_no - 1} streams")
    except Exception as e:
        log.error(f"Error generating TiviMate playlist: {e}")


async def main() -> None:
    """Main function to run the scraper and generate playlists"""
    log.info("Starting SPFIT scraper")

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            )
            try:
                await scrape(browser)
            finally:
                await browser.close()

        generate_playlists()

        log.info("Playlist generation completed")
        print(f"\n SPFIT Playlists generated successfully!")
        print(f"    VLC: {VLC_OUTPUT}")
        print(f"    TiviMate: {TIVIMATE_OUTPUT}")
        print(f"    Total streams: {len(urls)}")
    except Exception as e:
        log.error(f"Error in main execution: {e}")
        print(f"\n Error: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
