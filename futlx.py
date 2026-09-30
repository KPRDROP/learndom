import asyncio
import re
from collections.abc import KeysView
from itertools import chain
from urllib.parse import urljoin, quote

from .utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "FUTLX"

CACHE_FILE = Cache(TAG, exp=19_800)

BASE_URL = "https://www.futbol-x.top"

REFERER = "https://www.futbol-x.top/"
ORIGIN = "https://www.futbol-x.top"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

SPORT_CATEGORIES = [
    "football",
    "tennis",
    "basketball",
    "fights",
    "motorsports",
    "americanfootball",
    "nhl",
    "baseball",
    "rugby",
    "golf",
    "others",
    "wrestling",
    "darts",
]

SPORT_URLS = [
    urljoin(BASE_URL, f"api/{sport}.json") for sport in SPORT_CATEGORIES
]


async def get_events(cached_keys: KeysView[str]) -> dict[str, dict[str, str | float]]:
    events = {}

    tasks = [network.request(url, log=log) for url in SPORT_URLS]

    results = await asyncio.gather(*tasks)

    if not (
        api_data := [
            *chain.from_iterable(r.json().get("streams", {}) for r in results if r)
        ]
    ):
        return events

    now = Time.rn()

    ptrn = re.compile(r"^(https?:\/\/)?(www\.)?.*\.m3u8$", re.I)

    for event in api_data:
        if not (streams := event.get("streams")):
            continue

        for event_info in streams:
            if not all(
                values := [
                    event_info.get(k)
                    for k in (
                        "name",
                        "tag",
                        "starts_at",
                        "streams",
                    )
                ]
            ):
                continue

            name, sport, event_time, event_streams = values

            event_dt = Time.from_str(event_time, tz_name="MSK")

            if event_dt.date() != now.date():
                continue

            for stream_info in event_streams:
                if not (source := stream_info.get("url")):
                    continue

                elif not ptrn.search(source):
                    continue

                url_title = stream_info["title"]

                if (key := f"[{sport}] {name} | {url_title} ({TAG})") in cached_keys:
                    continue

                tvg_id, logo = leagues.get_tvg_info(sport, name)

                events[key] = {
                    "source": source,
                    "logo": logo,
                    "refer": REFERER,
                    "timestamp": now.timestamp(),
                    "tvg-id": tvg_id or "Live.Event.us",
                }

    return events


def build_vlc_output(events: dict[str, dict[str, str | float]]) -> str:
    lines = ["#EXTM3U"]

    for idx, (name, data) in enumerate(events.items(), start=1):
        logo = data.get("logo") or ""
        tvg_id = data.get("tvg-id") or "Live.Event.us"
        source = data["source"]
        refer = data.get("refer") or REFERER

        lines.append(
            f'#EXTINF:-1 tvg-chno="{idx}" tvg-id="{tvg_id}" '
            f'tvg-name="{name}" tvg-logo="{logo}" '
            f'group-title="Live Events",{name}'
        )
        lines.append(f"#EXTVLCOPT:http-referrer={refer}")
        lines.append(f"#EXTVLCOPT:http-origin={ORIGIN}")
        lines.append(f"#EXTVLCOPT:http-user-agent={USER_AGENT}")
        lines.append(source)

    return "\n".join(lines) + "\n"


def build_tivimate_output(events: dict[str, dict[str, str | float]]) -> str:
    lines = ["#EXTM3U"]

    encoded_ua = quote(USER_AGENT, safe="")

    for idx, (name, data) in enumerate(events.items(), start=1):
        logo = data.get("logo") or ""
        tvg_id = data.get("tvg-id") or "Live.Event.us"
        source = data["source"]
        refer = data.get("refer") or REFERER

        headers = (
            f"|referer={refer}"
            f"|origin={ORIGIN}"
            f"|user-agent={encoded_ua}"
        )

        lines.append(
            f'#EXTINF:-1 tvg-chno="{idx}" tvg-id="{tvg_id}" '
            f'tvg-name="{name}" tvg-logo="{logo}" '
            f'group-title="Live Events",{name}'
        )
        lines.append(f"{source}{headers}")

    return "\n".join(lines) + "\n"


def write_output_files(events: dict[str, dict[str, str | float]]) -> None:
    with open("futlx_vlc.m3u8", "w", encoding="utf-8") as f:
        f.write(build_vlc_output(events))

    with open("futlx_tivimate.m3u8", "w", encoding="utf-8") as f:
        f.write(build_tivimate_output(events))

    log.info("Wrote futlx_vlc.m3u8 and futlx_tivimate.m3u8")


async def scrape() -> None:
    cached_urls = CACHE_FILE.load()

    valid_count = len(
        valid_urls := {k: v for k, v in cached_urls.items() if v["source"]}
    )

    urls.update(valid_urls)

    log.info(f"Loaded {valid_count} event(s) from cache")

    log.info(f'Scraping from "{BASE_URL}"')

    urls.update(await get_events(cached_urls.keys()))

    (
        log.info(f"Collected and cached {new_count} new event(s)")
        if (new_count := len(urls) - valid_count)
        else log.info("No new events found")
    )

    CACHE_FILE.write(urls)

    write_output_files(urls)
