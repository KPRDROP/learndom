from collections.abc import KeysView
from dataclasses import dataclass
from datetime import datetime, timezone
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

BASE_DOMAIN = "reedstreams.link"

REFERER = "https://edgesport.cfd/"
ORIGIN = "https://edgesport.cfd"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/111.0.0.0 Safari/537.36"
)

VLC_FILE = "rdstrm_vlc.m3u8"
TIVIMATE_FILE = "rdstrm_tivimate.m3u8"

# Sports to include. Add/remove as needed. Empty set = include everything.
ALLOWED_CATEGORIES: set[str] = {
    "american-football",
    "baseball",
    "basketball",
    "hockey",
    "football",
    "soccer",
    "tennis",
    "mma",
    "boxing",
    "ufc",
    "motorsport",
    "racing",
    "golf",
    "cricket",
    "rugby",
    "volleyball",
    "handball",
    "esports",
}

# How wide the "live now" window should be.
WINDOW_MINUTES_BEFORE = 180
WINDOW_MINUTES_AFTER = 180


@dataclass(kw_only=True, slots=True)
class REEDEvent(Event):
    logo: str | None = None


def _to_epoch_seconds(value: Any) -> int | None:
    """Normalize date/start_time values to epoch seconds."""
    if value is None:
        return None

    # Numeric (int/float) — could be seconds or milliseconds
    if isinstance(value, (int, float)):
        v = int(value)
        # Heuristic: > 10^12 → milliseconds
        return v // 1000 if v > 10_000_000_000 else v

    # String — could be digits, ISO-8601, etc.
    if isinstance(value, str):
        s = value.strip()
        if s.isdigit():
            v = int(s)
            return v // 1000 if v > 10_000_000_000 else v

        # Try ISO formats
        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
        ):
            try:
                dt = datetime.strptime(s, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return int(dt.timestamp())
            except ValueError:
                continue

    return None


def _unwrap_api_payload(payload: Any) -> list[dict[str, Any]]:
    """Return a flat list of event dicts from whatever shape the API returns."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]

    if isinstance(payload, dict):
        for key in ("matches", "data", "events", "results", "items"):
            if isinstance(payload.get(key), list):
                return [x for x in payload[key] if isinstance(x, dict)]

    return []


async def pre_process(url: str, url_num: int) -> str | None:
    if not (event_data := await network.request(url, url_num, log=log)):
        return

    try:
        payload = event_data.json()
    except Exception as e:
        log.warning(f"URL {url_num}) JSON decode failed: {e}")
        return

    if not (streams := payload.get("streams")):
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

    api_data = API_FILE.load(per_entry=False, ts_index=-1)

    if not api_data:
        log.info("Refreshing API cache")

        api_url = urljoin(f"https://api.{BASE_DOMAIN}", "api/matches/all")

        if r := await network.request(api_url, log=log):
            try:
                raw = r.json()
            except Exception as e:
                log.error(f"API JSON decode failed: {e}")
                return events

            api_data = _unwrap_api_payload(raw)

            if not api_data:
                log.warning(f"API returned no usable events. Type={type(raw).__name__}")
                return events

            # Show a sample so we can debug the schema in CI logs
            sample = api_data[0]
            log.info(f"API sample keys: {sorted(sample.keys())}")

            API_FILE.write(api_data)
        else:
            log.warning("API request failed")
            return events

    start_dt = now.delta(minutes=-WINDOW_MINUTES_BEFORE)
    end_dt = now.delta(minutes=WINDOW_MINUTES_AFTER)

    for event in api_data:
        if not isinstance(event, dict):
            continue

        category = (
            event.get("category")
            or event.get("sport")
            or event.get("league")
            or ""
        ).lower()

        name = event.get("title") or event.get("name") or event.get("match")
        sport = event.get("league_name") or event.get("league") or category
        raw_date = (
            event.get("date")
            or event.get("start_time")
            or event.get("startTime")
            or event.get("timestamp")
        )
        stream_id = (
            event.get("id")
            or event.get("match_id")
            or event.get("stream_id")
            or event.get("slug")
        )

        if not all([name, raw_date, stream_id]):
            continue

        if ALLOWED_CATEGORIES and category and category not in ALLOWED_CATEGORIES:
            continue

        event_ts = _to_epoch_seconds(raw_date)
        if event_ts is None:
            continue

        event_dt = Time.from_ts(event_ts)

        if not start_dt <= event_dt <= end_dt:
            continue

        key = f"[{sport}] {name} ({TAG})"

        if key in cached_keys:
            continue

        poster = (
            event.get("poster")
            or event.get("image")
            or event.get("logo")
            or event.get("badge")
        )

        logo = (
            urljoin(f"https://api.{BASE_DOMAIN}", poster)
            if poster
            else None
        )

        events.append(
            REEDEvent(
                sport=str(sport),
                name=str(name),
                logo=logo,
                link=urljoin(
                    f"https://links.{BASE_DOMAIN}",
                    f"stream/{stream_id}",
                ),
                timestamp=event_ts,
            )
        )

    log.info(
        f"Matched {len(events)} event(s) in window "
        f"[-{WINDOW_MINUTES_BEFORE}m, +{WINDOW_MINUTES_AFTER}m]"
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
                event_link = None

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
        log.error(f"Fatal error during update: {e}")
        raise


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
