from collections.abc import KeysView
from dataclasses import dataclass
from functools import partial
from typing import Any
from urllib.parse import quote, urljoin

from playwright.async_api import Browser, async_playwright

from utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "RDSTRM"

CACHE_FILE = Cache(TAG, exp=10_800)

API_FILE = Cache(f"{TAG}-api", exp=28_800)

BASE_URL = "https://reedstreams.link/"

REFERER = "https://edgesport.cfd/"
ORIGIN = "https://edgesport.cfd"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/111.0.0.0 Safari/537.36"
)

VLC_FILE = "rdstrm_vlc.m3u8"
TIVIMATE_FILE = "rdstrm_tivimate.m3u8"


@dataclass(kw_only=True, slots=True)
class REEDEvent(Event):
    logo: str | None = None


async def pre_process(url: str, url_num: int) -> str | None:
    if not (event_data := await network.request(url, url_num, log=log)):
        return

    elif not (streams := event_data.json().get("streams")):
        log.warning(f"URL {url_num}) No streams available")
        return

    for stream in streams:
        if stream.get("source", "").lower() != "krishna":
            continue

        # elif stream.get("sourceName", "") != "Reed 1":
        #     continue

        if stream_url := stream.get("embedUrl"):
            return stream_url

    log.warning(f"URL {url_num}) No valid stream url found")
    return


async def get_events(cached_keys: KeysView[str]) -> list[REEDEvent]:
    now = Time.rn()

    events: list[REEDEvent] = []

    if not (api_data := API_FILE.load(per_entry=False, ts_index=-1)):
        log.info("Refreshing API cache")

        api_data = [{"timestamp": now.timestamp()}]

        if r := await network.request(
            urljoin(f"https://api.{BASE_URL}", "api/matches/all"),
            log=log,
        ):
            api_data: list[dict[str, Any]] = r.json()

            api_data[-1]["timestamp"] = now.timestamp()

        API_FILE.write(api_data)

    start_dt = now.delta(minutes=-30)
    end_dt = now.delta(minutes=30)

    for event in api_data:
        if not all(
            values := [
                event.get(x)
                for x in (
                    "category",
                    "title",
                    "league_name",
                    "date",
                    "id",
                )
            ]
        ):
            continue

        category, name, sport, start_ts, stream_id = values

        if category not in {
            "american-football",
            "baseball",
            # "basketball",
            # "hockey",
        }:
            continue

        event_dt = Time.from_ts(event_ts := int(f"{start_ts}"[:-3]))

        if not start_dt <= event_dt <= end_dt:
            continue

        elif f"[{sport}] {name} ({TAG})" in cached_keys:
            continue

        logo = (
            urljoin(f"https://api.{BASE_URL}", poster)
            if (poster := event.get("poster"))
            else None
        )

        events.append(
            REEDEvent(
                sport=sport,
                name=name,
                logo=logo,
                link=urljoin(f"https://links.{BASE_DOMAIN}", f"stream/{stream_id}"),
                timestamp=event_ts,
            )
        )

    return events


def write_outputs(cached_urls: dict[str, dict[str, str | float]]) -> None:
    vlc_lines: list[str] = ["#EXTM3U"]
    tivimate_lines: list[str] = ["#EXTM3U"]

    encoded_ua = quote(USER_AGENT, safe="")

    chno = 0

    for name, entry in cached_urls.items():
        source = entry.get("source")
        if not source:
            continue

        chno += 1

        logo = entry.get("logo") or ""
        tvg_id = entry.get("tvg-id") or "Live.Event.us"

        extinf = (
            f'#EXTINF:-1 tvg-chno="{chno}" tvg-id="{tvg_id}" '
            f'tvg-name="{name}" tvg-logo="{logo}" '
            f'group-title="Live Events",{name}'
        )

        vlc_lines.append(extinf)
        vlc_lines.append(f"#EXTVLCOPT:http-referrer={REFERER}")
        vlc_lines.append(f"#EXTVLCOPT:http-origin={ORIGIN}")
        vlc_lines.append(f"#EXTVLCOPT:http-user-agent={USER_AGENT}")
        vlc_lines.append(str(source))

        tivimate_lines.append(extinf)
        tivimate_lines.append(
            f"{source}|referer={REFERER}|origin={ORIGIN}|user-agent={encoded_ua}"
        )

    try:
        with open(VLC_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(vlc_lines) + "\n")
        log.info(f"Wrote {VLC_FILE} ({chno} entries)")

        with open(TIVIMATE_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(tivimate_lines) + "\n")
        log.info(f"Wrote {TIVIMATE_FILE} ({chno} entries)")

    except OSError as e:
        log.error(f"Failed to write output files: {e}")


async def scrape(browser: Browser) -> None:
    cached_urls = CACHE_FILE.load()

    valid_urls = {k: v for k, v in cached_urls.items() if v["source"]}

    valid_count = cached_count = len(valid_urls)

    urls.update(valid_urls)

    log.info(f"Loaded {cached_count} event(s) from cache")

    log.info(f'Scraping from "{network.ensure_https(f"//{BASE_DOMAIN}")}"')

    if events := await get_events(cached_urls.keys()):
        log.info(f"Processing {len(events)} new URL(s)")

        async with network.event_context(browser) as context:
            for i, ev in enumerate(events, start=1):
                source = None

                async with network.event_page(context) as page:
                    if event_link := await pre_process(ev.link, i):
                        handler = partial(
                            network.process_event,
                            url=event_link,
                            url_num=i,
                            page=page,
                            log=log,
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
                        "logo": ev.logo or logo,
                        "refer": event_link,
                        "timestamp": ev.timestamp,
                        "tvg-id": tvg_id or "Live.Event.us",
                    }

                    cached_urls[key] = entry

                    if source:
                        valid_count += 1

                        urls[key] = entry

        log.info(f"Collected and cached {valid_count - cached_count} new event(s)")

    else:
        log.info("No new events found")

    CACHE_FILE.write(cached_urls)

    write_outputs(cached_urls)


async def main() -> None:
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )

            try:
                await scrape(browser)
            finally:
                await browser.close()

    except Exception as e:
        log.error(f"Fatal error during updater: {e}")
        raise


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
