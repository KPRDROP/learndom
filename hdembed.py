import asyncio
from collections.abc import KeysView
from functools import partial
from urllib.parse import urljoin, quote

from playwright.async_api import Browser, Page, async_playwright

from utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "HDEMBED"

CACHE_FILE = Cache(TAG, exp=5_400)

API_FILE = Cache(f"{TAG}-api", exp=28_800)

BASE_URL = "https://rockystream.st"

# Output files
OUTPUT_VLC = "hdembed_vlc.m3u8"
OUTPUT_TIVIMATE = "hdembed_tivimate.m3u8"

# Headers for streams
REFERER = "https://forgemindly.com/"
ORIGIN = "https://forgemindly.com/"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"

# Encoded user agent for Tivimate
UA_ENC = quote(USER_AGENT, safe="")


def fix_league(s: str) -> str:
    splits = s.split()
    if not splits:
        return s
    i = splits[0]
    return f"{i.upper() if len(i) <= 5 else i.capitalize()} {' '.join(x.capitalize() for x in splits[1:])}".strip()


def clean_display_name(name: str) -> str:
    """Clean display name by removing commas and extra spaces."""
    import re
    if not name:
        return ""
    cleaned = re.sub(r',\s*', ' ', name)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


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
            timeout=6_000,
            referer=BASE_URL,
        )

        if not resp or resp.status != 200:
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


async def get_events(cached_keys: KeysView[str]) -> list[Event]:
    now = Time.rn()

    if not (api_data := API_FILE.load(per_entry=False, ts_index=-1)):
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
            event_name = clean_display_name(event["title"])

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
    
    vlc_lines = ["#EXTM3U"]
    tivimate_lines = ["#EXTM3U"]
    
    stream_count = 0
    channel_number = 1
    
    for key, data in events_data.items():
        if not data.get("source"):
            continue
            
        stream_url = data["source"]
        
        # Extract sport and name from key
        if "]" in key:
            sport_part = key.split("]")[0].replace("[", "").strip()
            name_part = key.split("]")[1].replace(f" ({TAG})", "").strip()
        else:
            sport_part = data.get("sport", "Unknown")
            name_part = key.replace(f" ({TAG})", "").strip()
        
        # Clean display name
        display_name = clean_display_name(f"[{sport_part}] {name_part} ({TAG})")
        
        tvg_id = data.get("tvg-id", "Live.Event.us")
        logo = data.get("logo", "")
        group_title = data.get("sport", sport_part)
        
        # VLC format with EXTVLCOPT options
        vlc_lines.append(
            f'#EXTINF:-1 tvg-chno="{channel_number}" '
            f'tvg-id="{tvg_id}" '
            f'tvg-name="{display_name}" '
            f'tvg-logo="{logo}" '
            f'group-title="{group_title}",{display_name}'
        )
        vlc_lines.append(f"#EXTVLCOPT:http-referrer={REFERER}")
        vlc_lines.append(f"#EXTVLCOPT:http-origin={ORIGIN}")
        vlc_lines.append(f"#EXTVLCOPT:http-user-agent={USER_AGENT}")
        vlc_lines.append(stream_url)
        
        # Tivimate format with pipe headers
        tivimate_lines.append(
            f'#EXTINF:-1 tvg-chno="{channel_number}" '
            f'tvg-id="{tvg_id}" '
            f'tvg-name="{display_name}" '
            f'tvg-logo="{logo}" '
            f'group-title="{group_title}",{display_name}'
        )
        tivimate_lines.append(
            f"{stream_url}|referer={REFERER}|origin={ORIGIN}|user-agent={UA_ENC}"
        )
        
        stream_count += 1
        channel_number += 1
    
    if stream_count == 0:
        log.warning("No streams to write to m3u8 files.")
        with open(OUTPUT_VLC, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
        with open(OUTPUT_TIVIMATE, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
        return
    
    try:
        with open(OUTPUT_VLC, "w", encoding="utf-8") as f:
            f.write("\n".join(vlc_lines))
        log.info(f"VLC playlist written to {OUTPUT_VLC} with {stream_count} streams")
    except Exception as e:
        log.error(f"Failed to write VLC playlist: {e}")
    
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

        # Use network.event_context and network.event_page which properly
        # handle adblock with service worker exception handling
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
                        "name": ev.name,
                    }

                    cached_urls[key] = entry

                    if source:
                        valid_count += 1
                        urls[key] = entry

        log.info(f"Collected and cached {valid_count - cached_count} new event(s)")

        # Write m3u8 files
        write_m3u8_files(cached_urls)

    else:
        log.info("No new events found")

    CACHE_FILE.write(cached_urls)


async def main():
    """Main entry point for the script."""
    log.info("Starting HDEmbed updater...")

    # FIX: Initialize adblock engine before using event_context
    await network.setup_adblock()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox']
        )
        try:
            await scrape(browser)
        finally:
            await browser.close()

    log.info("Updater completed.")


if __name__ == "__main__":
    asyncio.run(main())
