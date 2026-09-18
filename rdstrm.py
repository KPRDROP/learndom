from collections.abc import KeysView
from dataclasses import dataclass
from functools import partial
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urljoin, urlsplit

from playwright.async_api import Browser, async_playwright

from utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

TAG = "RDSTRM"

BASE_DOMAIN = "reedstreams.link"
BASE_URL = "https://reedstreams.link/"
SITE_URL = "https://reedstreams.to/"
API_BASE_URL = "https://api.reedstreams.link/api/"
EVENTS_API_URL = "https://api.reedstreams.link/api/matches/all"
LINKS_BASE_URL = "https://links.reedstreams.link/"

REFERER = "https://edgesport.cfd/"
ORIGIN = "https://edgesport.cfd"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/111.0.0.0 Safari/537.36"
)
EVENT_START_OFFSET_MINUTES = -180
EVENT_END_OFFSET_MINUTES = 180
CACHE_FILE = Cache(TAG, exp=10_800)
API_FILE = Cache(f"{TAG}-api", exp=28_800)
urls: dict[str, dict[str, Any]] = {}


@dataclass(kw_only=True, slots=True)
class REEDEvent(Event):
    logo: str | None = None


REQUEST_HEADERS = {
    "Referer": REFERER,
    "Origin": ORIGIN,
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;"
        "q=0.8,application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "DNT": "1",
}


def normalize_url(url: str | None) -> str | None:
    if not url:
        return None

    url = str(url).strip()

    if not url:
        return None

    if url.startswith("//"):
        return f"https:{url}"

    if url.startswith("http://") or url.startswith("https://"):
        return url

    return urljoin(LINKS_BASE_URL, url.lstrip("/"))


def encoded_user_agent() -> str:
    return quote(USER_AGENT, safe="")


def build_vlc_entry(
    key: str,
    entry: dict[str, Any],
) -> str | None:
    source = entry.get("source")

    if not source:
        return None

    source = str(source).strip()

    if not source:
        return None

    tvg_id = entry.get("tvg-id") or "Live.Event.us"
    tvg_name = entry.get("tvg-name") or key
    tvg_logo = entry.get("logo") or ""
    group_title = entry.get("group-title") or "Live Events"
    tvg_chno = entry.get("tvg-chno")
    chno = f' tvg-chno="{tvg_chno}"' if tvg_chno else ""

    lines = [
        (
            f'#EXTINF:-1{chno} '
            f'tvg-id="{tvg_id}" '
            f'tvg-name="{tvg_name}" '
            f'tvg-logo="{tvg_logo}" '
            f'group-title="{group_title}",'
            f'{tvg_name}'
        ),
        f"#EXTVLCOPT:http-referrer={REFERER}",
        f"#EXTVLCOPT:http-origin={ORIGIN}",
        f"#EXTVLCOPT:http-user-agent={USER_AGENT}",
        source,
    ]

    return "\n".join(lines)


def build_tivimate_entry(
    key: str,
    entry: dict[str, Any],
) -> str | None:
    source = entry.get("source")

    if not source:
        return None

    source = str(source).strip()

    if not source:
        return None

    tvg_id = entry.get("tvg-id") or "Live.Event.us"
    tvg_name = entry.get("tvg-name") or key
    tvg_logo = entry.get("logo") or ""
    group_title = entry.get("group-title") or "Live Events"
    tvg_chno = entry.get("tvg-chno")
    chno = f' tvg-chno="{tvg_chno}"' if tvg_chno else ""

    extinf = (
        f'#EXTINF:-1{chno} '
        f'tvg-id="{tvg_id}" '
        f'tvg-name="{tvg_name}" '
        f'tvg-logo="{tvg_logo}" '
        f'group-title="{group_title}",'
        f'{tvg_name}'
    )

    ua = encoded_user_agent()

    stream_line = (
        f"{source}"
        f"|referer={REFERER}"
        f"|origin={ORIGIN}"
        f"|user-agent={ua}"
    )

    return f"{extinf}\n{stream_line}"


def write_playlists() -> None:
    vlc_entries: list[str] = []
    tivimate_entries: list[str] = []

    sorted_urls = sorted(
        urls.items(),
        key=lambda item: (
            item[1].get("timestamp", 0),
            item[0].lower(),
        ),
    )

    for key, entry in sorted_urls:
        if not entry.get("source"):
            continue

        vlc_entry = build_vlc_entry(key, entry)

        if vlc_entry:
            vlc_entries.append(vlc_entry)

        tivimate_entry = build_tivimate_entry(key, entry)

        if tivimate_entry:
            tivimate_entries.append(tivimate_entry)

    vlc_content = "#EXTM3U\n"

    if vlc_entries:
        vlc_content += "\n".join(vlc_entries) + "\n"

    tivimate_content = "#EXTM3U\n"

    if tivimate_entries:
        tivimate_content += "\n".join(tivimate_entries) + "\n"

    with open(
        "rdstrm_vlc.m3u8",
        "w",
        encoding="utf-8",
        newline="\n",
    ) as f:
        f.write(vlc_content)

    with open(
        "rdstrm_tivimate.m3u8",
        "w",
        encoding="utf-8",
        newline="\n",
    ) as f:
        f.write(tivimate_content)

    log.info(
        f"Wrote rdstrm_vlc.m3u8 "
        f"({len(vlc_entries)} entries)"
    )

    log.info(
        f"Wrote rdstrm_tivimate.m3u8 "
        f"({len(tivimate_entries)} entries)"
    )


async def pre_process(
    url: str,
    url_num: int,
) -> str | None:
    url = normalize_url(url)

    if not url:
        log.warning(f"URL {url_num}) Invalid stream endpoint")
        return None

    try:
        event_data = await network.request(
            url,
            url_num,
            log=log,
        )
    except Exception as exc:
        log.error(
            f"URL {url_num}) Failed stream endpoint request: {exc}"
        )
        return None

    if not event_data:
        log.warning(f"URL {url_num}) No response from stream endpoint")
        return None

    try:
        payload = event_data.json()
    except Exception as exc:
        log.error(f"URL {url_num}) Invalid JSON response: {exc}")
        return None

    if not isinstance(payload, dict):
        log.warning(f"URL {url_num}) Unexpected response format")
        return None

    streams = payload.get("streams")

    if not streams:
        log.warning(f"URL {url_num}) No streams available")
        return None

    if not isinstance(streams, list):
        log.warning(f"URL {url_num}) Invalid streams format")
        return None

    stream_urls = [
        stream.get("embedUrl")
        for stream in streams
        if isinstance(stream, dict)
        and stream.get("source") in ("tnasty", "admin", "hotel")
        and stream.get("embedUrl")
    ]

    if not stream_urls:
        log.warning(f"URL {url_num}) No valid stream url found")
        return None

    stream_url = stream_urls[0]

    try:
        m3u = dict(parse_qsl(urlsplit(stream_url).query)).get("url")
    except Exception as exc:
        log.warning(f"URL {url_num}) Failed to parse url: {exc}")
        return None

    if not m3u:
        log.warning(f"URL {url_num}) Failed to parse url")
        return None

    log.info(f"URL {url_num}) Captured M3U8")
    return unquote(m3u)


async def load_api_data(now: Any) -> list[dict[str, Any]]:
    api_data = API_FILE.load(
        per_entry=False,
        ts_index=-1,
    )

    if api_data:
        if isinstance(api_data, list):
            return api_data

    log.info("Refreshing API cache")

    response = await network.request(
        EVENTS_API_URL,
        log=log,
    )

    if not response:
        log.error(
            f"Unable to fetch events API: {EVENTS_API_URL}"
        )
        return []

    try:
        data = response.json()
    except Exception as exc:
        log.error(
            f"Events API returned invalid JSON: {exc}"
        )
        return []

    if not isinstance(data, list):
        log.error(
            "Events API response is not a list"
        )
        return []

    if data and isinstance(data[0], dict):
        log.info(
            f"API sample keys: {list(data[0].keys())}"
        )

    timestamp = now.timestamp()

    if data:
        data[-1]["timestamp"] = timestamp

    API_FILE.write(data)

    return data


async def get_events(
    cached_keys: KeysView[str],
) -> list[REEDEvent]:
    now = Time.rn()
    events: list[REEDEvent] = []
    api_data = await load_api_data(now)

    if not api_data:
        return events

    start_dt = now.delta(
        minutes=EVENT_START_OFFSET_MINUTES
    )

    end_dt = now.delta(
        minutes=EVENT_END_OFFSET_MINUTES
    )

    matched_count = 0
    cached_key_set = set(cached_keys)

    for event in api_data:
        if not isinstance(event, dict):
            continue

        values = [
            event.get(x)
            for x in (
                "category",
                "title",
                "league_name",
                "date",
                "id",
            )
        ]

        if not all(values):
            continue

        category, name, sport, start_ts, stream_id = values

        if category not in {
            "american-football",
            "baseball",
            # "basketball",
            # "hockey",
            "other",
            "fight",
            "motor-sports",
            "rugby",
        }:
            continue

        try:
            event_ts = int(str(start_ts)[:-3])
        except (TypeError, ValueError):
            log.debug(
                f"Skipping event with invalid timestamp: "
                f"{start_ts}"
            )
            continue

        try:
            event_dt = Time.from_ts(event_ts)
        except Exception:
            log.debug(
                f"Skipping event with invalid event time: "
                f"{start_ts}"
            )
            continue

        if not start_dt <= event_dt <= end_dt:
            continue

        matched_count += 1
        key = f"[{sport}] {name} ({TAG})"

        if key in cached_key_set:
            continue

        poster = event.get("poster")

        logo = (
            urljoin(
                API_BASE_URL,
                str(poster).lstrip("/"),
            )
            if poster
            else None
        )

        stream_link = urljoin(
            LINKS_BASE_URL,
            f"stream/{stream_id}",
        )

        events.append(
            REEDEvent(
                sport=sport,
                name=name,
                logo=logo,
                link=stream_link,
                timestamp=event_ts,
            )
        )

    log.info(
        f"Matched {matched_count} event(s) in window "
        f"[{EVENT_START_OFFSET_MINUTES}m, "
        f"+{EVENT_END_OFFSET_MINUTES}m]"
    )

    return events


async def scrape(browser: Browser) -> None:
    cached_urls = CACHE_FILE.load()

    if not isinstance(cached_urls, dict):
        cached_urls = {}

    valid_urls = {
        key: value
        for key, value in cached_urls.items()
        if isinstance(value, dict)
        and value.get("source")
    }

    cached_count = len(valid_urls)

    urls.clear()
    urls.update(valid_urls)

    log.info(
        f"Loaded {cached_count} event(s) from cache"
    )

    log.info(
        f'Scraping from "{BASE_URL}"'
    )

    events = await get_events(
        cached_urls.keys()
    )

    if not events:
        log.info("No new events found")
        write_playlists()
        return

    log.info(
        f"Processing {len(events)} new URL(s)"
    )

    valid_count = cached_count

    async with network.event_context(browser) as context:
        for i, ev in enumerate(events, start=1):
            source: str | None = None
            event_link: str | None = None

            if ev.link:
                event_link = normalize_url(ev.link)

            if not event_link:
                log.warning(
                    f"URL {i}) Missing event link"
                )
            else:
                try:
                    async with network.event_page(
                        context
                    ) as page:
                        embed_url = await pre_process(
                            event_link,
                            i,
                        )

                        if embed_url:
                            handler = partial(
                                network.process_event,
                                url=embed_url,
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

                except Exception as exc:
                    log.error(
                        f"URL {i}) Stream processing failed: "
                        f"{exc}"
                    )

            key = (
                f"[{ev.sport}] "
                f"{ev.name} "
                f"({TAG})"
            )

            tvg_id, league_logo = leagues.get_tvg_info(
                ev.sport,
                ev.name,
            )

            previous_entry = cached_urls.get(key)
            tvg_chno = None

            if isinstance(previous_entry, dict):
                tvg_chno = previous_entry.get("tvg-chno")

            entry = {
                "source": source,
                "logo": ev.logo or league_logo,
                "refer": event_link,
                "timestamp": ev.timestamp,
                "tvg-id": tvg_id or "Live.Event.us",
                "tvg-name": key,
                "group-title": "Live Events",
            }

            if tvg_chno:
                entry["tvg-chno"] = tvg_chno

            cached_urls[key] = entry

            if source:
                valid_count += 1
                urls[key] = entry

                log.info(
                    f"URL {i}) Added stream for {key}"
                )
            else:
                log.warning(
                    f"URL {i}) No playable stream captured"
                )

    new_count = valid_count - cached_count

    log.info(
        f"Collected and cached {new_count} new event(s)"
    )

    CACHE_FILE.write(cached_urls)
    write_playlists()


async def main() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
        )

        try:
            await scrape(browser)
        finally:
            await browser.close()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
