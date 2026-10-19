import base64
import re
from collections.abc import KeysView
from dataclasses import dataclass
from functools import partial
from typing import Any
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "SPORTSA"

CACHE_FILE = Cache(TAG, exp=10_800)

API_FILE = Cache(f"{TAG}-api", exp=19_800)

BASE_URL = "https://aurasports.xyz"

# Encoded UA used for TiviMate pipe headers (already URL-encoded)
TIVIMATE_UA = (
    "Mozilla%2F5.0%20(Windows%20NT%2010.0%3B%20Win64%3B%20x64)%20"
    "AppleWebKit%2F537.36%20(KHTML%2C%20like%20Gecko)%20"
    "Chrome%2F111.0.0.0%20Safari%2F537.36"
)

# Raw UA (decoded) used for VLC EXTVLCOPT
VLC_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.0.0 Safari/537.36"

DEFAULT_REFERER = BASE_URL


@dataclass(kw_only=True, slots=True)
class AURAEvent(Event):
    logo: str | None = None


def fix_league(s: str) -> str:
    splits = s.split()

    i = splits[0]

    return f"{i.upper() if len(i) <= 5 else i.capitalize()} {' '.join(x.capitalize() for x in splits[1:])}".strip()


async def process_event(url: str, url_num: int) -> tuple[str | None, str | None]:
    nones = None, None

    if not (
        event_data := await network.request(
            url,
            url_num,
            headers={"Referer": BASE_URL},
            log=log,
        )
    ):
        return nones

    soup = HTMLParser(event_data.content)

    ifr = soup.css_first("iframe.stream-frame")

    if not ifr or not (src := ifr.attributes.get("src")):
        log.warning(f"URL {url_num}) No iframe element found")
        return nones

    if not src.startswith("http"):
        splits = urlsplit(url)

        src = urlunsplit(splits._replace(path=src))

    if not (
        ifr_src_data := await network.request(
            src,
            url_num,
            headers={"Referer": BASE_URL},
            log=log,
        )
    ):
        return nones

    pattern = re.compile(r'activeStream\s?=\s?"([^"]*)";', re.I)

    if not (match := pattern.search(ifr_src_data.text)):
        log.warning(f"URL {url_num}) No Clappr source found.")
        return nones

    elif not (source := base64.b64decode(match[1]).decode("utf-8").strip()):
        log.warning(f"URL {url_num}) No Clappr source found.")
        return nones

    log.info(f"URL {url_num}) Captured M3U8")

    return source, f"{ifr_src_data.url}"


async def get_events(cached_keys: KeysView[str]) -> list[AURAEvent]:
    now = Time.rn()

    events: list[AURAEvent] = []

    if not (api_data := API_FILE.load(per_entry=False, ts_index=-1)):
        log.info("Refreshing API cache")

        api_data = [{"timestamp": now.timestamp()}]

        if r := await network.request(urljoin(BASE_URL, "api/schedule"), log=log):
            api_data: list[dict[str, Any]] = r.json()

            api_data[-1]["timestamp"] = now.timestamp()

        API_FILE.write(api_data)

    start_dt = now.delta(minutes=-60)
    end_dt = now.delta(minutes=60)

    for category in api_data:
        for event in category.get("schedule", []):
            if not all(
                values := [
                    event.get(x)
                    for x in (
                        "title",
                        "category",
                        "date",
                        "iframeUrl",
                    )
                ]
            ):
                continue

            name, sport, start_ts, link = values

            sport = (
                league_name
                if (league_name := event.get("league_name"))
                else fix_league(sport)
            )

            event_dt = Time.from_ts(event_ts := int(f"{start_ts}"[:-3]))

            if not start_dt <= event_dt <= end_dt:
                continue

            elif f"[{sport}] {name} ({TAG})" in cached_keys:
                continue

            events.append(
                AURAEvent(
                    sport=sport,
                    name=name,
                    logo=event.get("thumbnail"),
                    link=link,
                    timestamp=event_ts,
                )
            )

    return events


async def scrape() -> None:
    cached_sources = CACHE_FILE.load()

    valid_sources = {k: v for k, v in cached_sources.items() if v["source"]}

    valid_count = cached_count = len(valid_sources)

    urls.update(valid_sources)

    log.info(f"Loaded {cached_count} event(s) from cache")

    log.info(f'Scraping from "{BASE_URL}"')

    if events := await get_events(cached_sources.keys()):
        log.info(f"Processing {len(events)} new URL(s)")

        for i, ev in enumerate(events, start=1):
            handler = partial(
                process_event,
                url=ev.link,
                url_num=i,
            )

            source, iframe = await network.safe_process(
                handler,
                url_num=i,
                timeout_return=(None, None),
                semaphore=network.HTTP_S,
                log=log,
            )

            key = f"[{ev.sport}] {ev.name} ({TAG})"

            tvg_id, logo = leagues.get_tvg_info(ev.sport, ev.name)

            entry = {
                "source": source,
                "logo": ev.logo or logo,
                "refer": iframe,
                "timestamp": ev.timestamp,
                "tvg-id": tvg_id or "Live.Event.us",
                "link": ev.link,
            }

            cached_sources[key] = entry

            if source:
                valid_count += 1

                urls[key] = entry

        log.info(f"Collected and cached {valid_count - cached_count} new event(s)")

    else:
        log.info("No new events found")

    CACHE_FILE.write(cached_sources)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------
def _build_vlc_url(source: str, referer: str, ua: str) -> str:
    return f"{source}|referer={referer}|user-agent={ua}"


def _build_tivimate_url(source: str, referer: str, encoded_ua: str) -> str:
    return f"{source}|referer={referer}|user-agent={encoded_ua}"


def _sorted_entries() -> list[tuple[str, dict[str, Any]]]:
    return list(urls.items())


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------
def write_vlc_m3u8(filename: str = "sportaura_vlc.m3u8") -> None:
    """Write sportaura_vlc.m3u8 with EXTVLCOPT headers and raw UA."""
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

                f.write(
                    f'#EXTINF:-1 tvg-chno="{chno}" tvg-id="{tvg_id}" '
                    f'tvg-name="{key}" tvg-logo="{logo}" '
                    f'group-title="Live Events",{key}\n'
                )
                f.write(f"#EXTVLCOPT:http-referrer={referer}\n")
                f.write(f"#EXTVLCOPT:http-user-agent={VLC_UA}\n")
                f.write(_build_vlc_url(source, referer, VLC_UA) + "\n")

        log.info(f"Wrote {filename} ({len(urls)} event(s))")
    except Exception as e:
        log.error(f"Failed to write {filename}: {e}")


def write_tivimate_m3u8(filename: str = "sportaura_tivimate.m3u8") -> None:
    """Write sportaura_tivimate.m3u8 with pipe headers and URL-encoded UA."""
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

                f.write(
                    f'#EXTINF:-1 tvg-chno="{chno}" tvg-id="{tvg_id}" '
                    f'tvg-name="{key}" tvg-logo="{logo}" '
                    f'group-title="Live Events",{key}\n'
                )
                f.write(_build_tivimate_url(source, referer, TIVIMATE_UA) + "\n")

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
    await scrape()
    write_outputs()


def run() -> None:
    import asyncio
    asyncio.run(main())


if __name__ == "__main__":
    run()
