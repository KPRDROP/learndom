import asyncio
from collections.abc import KeysView
from functools import partial
from urllib.parse import urljoin

from playwright.async_api import Browser, Page

from utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "HDEMBED"

CACHE_FILE = Cache(TAG, exp=5_400)

API_FILE = Cache(f"{TAG}-api", exp=28_800)

BASE_URL = "https://embedhd.st"

# Output files
OUTPUT_VLC = "hdembed_vlc.m3u8"
OUTPUT_TIVIMATE = "hdembed_tivimate.m3u8"

# Headers for streams
REFERER = "https://forgemindly.com/"
ORIGIN = "https://forgemindly.com/"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"


def fix_league(s: str) -> str:
    splits = s.split()

    i = splits[0]

    return f"{i.upper() if len(i) <= 5 else i.capitalize()} {' '.join(x.capitalize() for x in splits[1:])}".strip()


async def process_event(
    url: str,
    url_num: int,
    page: Page,
) -> str | None:

    captured: list[str] = []

    got_one = asyncio.Event()

    handler = partial(
        network.capture_req,
        captured=captured,
        got_one=got_one,
    )

    page.on("request", handler)

    try:
        resp = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=10_000,  # Increased timeout
            referer=BASE_URL,
        )

        if not resp or resp.status != 200:
            log.error(f"URL {url_num}) Status Code: {resp.status if resp else 'None'}")
            return

        wait_task = asyncio.create_task(got_one.wait())

        try:
            await asyncio.wait_for(wait_task, timeout=8)  # Increased timeout
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


async def get_events(cached_keys: KeysView[str]) -> list[Event]:
    now = Time.rn()

    if not (api_data := API_FILE.load(per_entry=False)):
        log.info("Refreshing API cache")

        api_data = {"timestamp": now.timestamp()}

        if r := await network.request(urljoin(BASE_URL, "api-event.php"), log=log):
            api_data: dict = r.json()

            api_data["timestamp"] = now.timestamp()

        API_FILE.write(api_data)

    events: list[Event] = []

    start_dt = now.delta(hours=-3)
    end_dt = now.delta(minutes=30)

    for info in api_data.get("days", []):
        for event in info["items"]:
            if (event_league := event["league"]) == "channel tv":
                continue

            event_dt = Time.from_ts(event["ts_et"])

            if not start_dt <= event_dt <= end_dt:
                continue

            sport = fix_league(event_league)

            event_name = event["title"]

            if f"[{sport}] {event_name} ({TAG})" in cached_keys:
                continue

            if not (event_streams := event["streams"]):
                continue

            elif not (event_link := event_streams[0].get("link")):
                continue

            events.append(
                Event(
                    sport=sport,
                    name=event_name,
                    link=event_link,
                    timestamp=now.timestamp(),
                )
            )

    return events


def write_m3u8_files(events_data: dict[str, dict]) -> None:
    """Write the collected events to VLC and Tivimate m3u8 files."""
    
    # VLC format (with EXTVLCOPT options)
    vlc_lines = ["#EXTM3U"]
    
    # Tivimate format (with pipe headers)
    tivimate_lines = ["#EXTM3U"]
    
    # Count valid streams
    stream_count = 0
    channel_number = 1
    
    for key, data in events_data.items():
        if not data.get("source"):
            continue
            
        stream_url = data["source"]
        
        # Extract sport and name from key
        # Format: "[Sport] Match Name (TAG)"
        if "]" in key:
            sport_part = key.split("]")[0].replace("[", "").strip()
            name_part = key.split("]")[1].replace(f" ({TAG})", "").strip()
        else:
            sport_part = "Unknown"
            name_part = key.replace(f" ({TAG})", "").strip()
        
        # Get logo and tvg-id
        tvg_id = data.get("tvg-id", "Live.Event.us")
        logo = data.get("logo", "")
        league = data.get("sport", sport_part)
        
        # Create display name
        display_name = f"[{sport_part}] {name_part} ({TAG})"
        
        # VLC format with EXTVLCOPT options
        vlc_lines.append(f'#EXTINF:-1 tvg-chno="{channel_number}" tvg-id="{tvg_id}" tvg-name="{display_name}" tvg-logo="{logo}" group-title="Live Events",{display_name}')
        vlc_lines.append(f"#EXTVLCOPT:http-referrer={REFERER}")
        vlc_lines.append(f"#EXTVLCOPT:http-origin={ORIGIN}")
        vlc_lines.append(f"#EXTVLCOPT:http-user-agent={USER_AGENT}")
        vlc_lines.append(stream_url)
        
        # Tivimate format with pipe headers
        encoded_ua = USER_AGENT.replace("%", "%25").replace(" ", "%20")
        tivimate_line = f"{stream_url}|referer={REFERER}|origin={ORIGIN}|user-agent={encoded_ua}"
        tivimate_lines.append(f'#EXTINF:-1 tvg-chno="{channel_number}" tvg-id="{tvg_id}" tvg-name="{display_name}" tvg-logo="{logo}" group-title="Live Events",{display_name}')
        tivimate_lines.append(tivimate_line)
        
        stream_count += 1
        channel_number += 1
    
    if stream_count == 0:
        log.warning("No streams to write to m3u8 files.")
        # Create empty files with just the header
        with open(OUTPUT_VLC, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
        with open(OUTPUT_TIVIMATE, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
        return
    
    # Write VLC file
    try:
        with open(OUTPUT_VLC, "w", encoding="utf-8") as f:
            f.write("\n".join(vlc_lines))
        log.info(f"VLC playlist written to {OUTPUT_VLC} with {stream_count} streams")
    except Exception as e:
        log.error(f"Failed to write VLC playlist: {e}")
    
    # Write Tivimate file
    try:
        with open(OUTPUT_TIVIMATE, "w", encoding="utf-8") as f:
            f.write("\n".join(tivimate_lines))
        log.info(f"Tivimate playlist written to {OUTPUT_TIVIMATE} with {stream_count} streams")
    except Exception as e:
        log.error(f"Failed to write Tivimate playlist: {e}")


async def scrape(browser: Browser) -> None:
    cached_urls = CACHE_FILE.load()

    valid_urls = {k: v for k, v in cached_urls.items() if v.get("source")}

    valid_count = cached_count = len(valid_urls)

    urls.update(valid_urls)

    log.info(f"Loaded {cached_count} event(s) from cache")

    log.info(f'Scraping from "{BASE_URL}"')

    if events := await get_events(cached_urls.keys()):
        log.info(f"Processing {len(events)} new URL(s)")

        # Create a simple browser context without adblocker
        # This bypasses the adblock issues in webwork.py
        context = await browser.new_context(
            user_agent=network.UA,
            viewport={"width": 1366, "height": 768},
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Upgrade-Insecure-Requests": "1",
            }
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
                    )

                    source = await network.safe_process(
                        handler,
                        url_num=i,
                        semaphore=network.PW_S,
                        log=log,
                    )

                    tvg_id, logo = leagues.get_tvg_info(ev.sport, ev.name)

                    key = f"[{ev.sport}] {ev.name} ({TAG})"

                    entry = {
                        "source": source,
                        "logo": logo,
                        "refer": REFERER,
                        "timestamp": ev.timestamp,
                        "tvg-id": tvg_id or "Live.Event.us",
                        "link": ev.link,
                        "sport": ev.sport,
                    }

                    cached_urls[key] = entry

                    if source:
                        valid_count += 1
                        urls[key] = entry
                finally:
                    await page.close()
        finally:
            await context.close()

        log.info(f"Collected and cached {valid_count - cached_count} new event(s)")
        
        # Write m3u8 files
        write_m3u8_files(cached_urls)

    else:
        log.info("No new events found")

    CACHE_FILE.write(cached_urls)


async def main():
    """Main entry point for the script."""
    from playwright.async_api import async_playwright
    
    log.info("Starting HDEmbed scraper...")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            await scrape(browser)
        finally:
            await browser.close()
    
    log.info("Scraping completed.")


if __name__ == "__main__":
    asyncio.run(main())
