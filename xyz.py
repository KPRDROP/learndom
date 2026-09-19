import asyncio
import re
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit, quote

from utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "XYZ"

CACHE_FILE = Cache(TAG, exp=28_800)

API_FILE = Cache(f"{TAG}-api", exp=28_800)

BASE_URL, TOKEN_API = "https://xyzstreams.st/", "https://dlhd.net"

# Output files
VLC_OUTPUT = "xyz_vlc.m3u8"
TIVIMATE_OUTPUT = "xyz_tivimate.m3u8"

# Headers for streams
REFERER = "https://xyzstreams.st/"
ORIGIN = "https://xyzstreams.st"

# Sport endpoints - all categories
SPORT_URLS = {
    "MLB": "mlb.html",
    "NFL": "nfl.html",
    "NBA": "nba.html",
    "WNBA": "wnba.html",
    "NHL": "nhl.html",
    "American Football": "cfb.html",
    "Fighting": "ufc.html",
}

# Special event URL pattern
SPECIAL_EVENT_PATTERN = "i-stream-{}.html"

# User agents
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 10; K) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Mobile Safari/537.36"
)
TIVIMATE_USER_AGENT = USER_AGENT

# ESPN API URLs for event data
API_URLS = [
    urljoin("https://site.api.espn.com/apis/site/v2/sports/", f"{sport}/scoreboard")
    for sport in [
        "baseball/mlb",
        "football/nfl",
        "basketball/nba",
        "basketball/wnba",
        "hockey/nhl",
    ]
]


def tokenize(s: str, token: str) -> str:
    """Add token to stream URL"""
    p = urlsplit(TOKEN_API)
    splits = urlsplit(s)
    new = splits._replace(
        scheme=p.scheme,
        netloc=p.netloc,
        query=f"token={token}",
    )
    return urlunsplit(new)


def normalize_url(url: str | None) -> str:
    """Normalize a URL by adding https:// if needed."""
    if not url:
        return ""
    url = str(url).strip()
    if not url:
        return ""
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith(("http://", "https://")):
        return url
    return f"https://{url}"


async def refresh_api_cache(now: Time) -> list[dict[str, Any]]:
    """Fetch event data from ESPN APIs"""
    tasks = [
        network.request(
            url,
            params={"dates": f"{now:%Y%m%d}"},
            headers={"User-Agent": "curl/8.20.0"},
            log=log,
        )
        for url in API_URLS
    ]

    results = await asyncio.gather(*tasks)

    api_data = []

    for resp in (r for r in results if r):
        try:
            data = resp.json()
            league = data["leagues"][0]["abbreviation"].upper()

            for event in data.get("events", []):
                event["league"] = league
                api_data.append(event)
        except Exception:
            continue

    if not api_data:
        return [{"timestamp": now.timestamp()}]

    api_data[-1]["timestamp"] = now.timestamp()

    return api_data


async def get_sports_map() -> dict[str, dict[str, dict[str, str]]]:
    """Scrape sport pages and extract M3U8_CHANNELS_MAP"""
    sports_map: dict[str, dict[str, dict[str, str]]] = {}

    tasks = [
        network.request(urljoin(BASE_URL, endpoint), log=log)
        for endpoint in SPORT_URLS.values()
    ]

    results = await asyncio.gather(*tasks)

    if not (texts := [(html.text, html.url) for html in results if html]):
        return sports_map

    # Abbreviation replacements
    replaces = {
        "MLB": {
            "CWS": "CHW",
            "AZ": "ARI",
        },
        "NFL": {
            "WAS": "WSH",
        },
    }

    ptrn = re.compile(r"M3U8_CHANNELS_MAP\s*=\s*\{(.*?)\};", re.S)

    for text, url in texts:
        sport = next(
            (k for k, v in SPORT_URLS.items() if url.endswith(v)),
            "Live Event"
        )

        if not (match := ptrn.search(text)):
            sports_map[sport] = {}
        else:
            pairs: list[tuple[str, str]] = re.findall(
                r"'([^']+)'\s*:\s*'([^']+)'",
                match[1],
            )
            sports_map[sport] = dict(pairs)

    for sport, abbrs in replaces.items():
        if sport in sports_map:
            for old, new in abbrs.items():
                if old in sports_map[sport]:
                    sports_map[sport][new] = sports_map[sport].pop(old)

    return sports_map


async def get_special_events() -> dict[str, str]:
    """Scrape special i-stream pages for additional events"""
    special_events = {}

    # Try i-stream pages 1-20
    for i in range(1, 21):
        url = urljoin(BASE_URL, SPECIAL_EVENT_PATTERN.format(i))
        try:
            response = await network.request(url, log=log)
            if not response:
                continue

            text = response.text

            # Look for M3U8_CHANNELS_MAP in special pages
            ptrn = re.compile(r"M3U8_CHANNELS_MAP\s*=\s*\{(.*?)\};", re.S)
            if match := ptrn.search(text):
                pairs = re.findall(
                    r"'([^']+)'\s*:\s*'([^']+)'",
                    match[1],
                )
                special_events.update(dict(pairs))
                log.info(f"Found {len(pairs)} channels on {url}")

        except Exception:
            continue

    return special_events


async def get_events() -> dict[str, dict[str, str | float]]:
    """Get all events with stream URLs"""
    now = Time.rn()

    events: dict[str, dict[str, str | float]] = {}

    # Step 1: Get token
    if not (
        token_data := await network.request(
            urljoin(TOKEN_API, "/api/token"),
            headers={"Referer": BASE_URL},
            log=log,
        )
    ):
        log.warning("Failed to get token")
        return events

    try:
        token = token_data.json().get("token")
        if not token:
            log.warning("No token found")
            return events
        log.info(f"Got token: {token[:20]}...")
    except Exception as e:
        log.error(f"Failed to parse token: {e}")
        return events

    # Step 2: Get API cache
    if not (api_data := API_FILE.load(per_entry=False, ts_index=-1)):
        log.info("Refreshing API cache")
        api_data = await refresh_api_cache(now)
        API_FILE.write(api_data)

    # Step 3: Get sports map from all sport pages
    sports_map = await get_sports_map()

    # Step 4: Get special events
    special_events = await get_special_events()
    if special_events:
        sports_map["Live Event"] = special_events

    if not sports_map:
        log.warning("No sports map found")
        return events

    # Step 5: Process ESPN events
    for game_info in api_data:
        if not isinstance(game_info, dict):
            continue

        if not all(
            values := [
                game_info.get(x)
                for x in (
                    "league",
                    "name",
                    "shortName",
                )
            ]
        ):
            continue

        sport, name, short_name = values

        tvg_id, logo = leagues.get_tvg_info(sport, name)

        for abbr in re.sub(r"(@|VS)", "", short_name, flags=re.I).split():
            key = f"[{sport}] {name} | {abbr} Feed ({TAG})"

            if source := sports_map.get(sport, {}).get(abbr):
                source = tokenize(source, token)

            events[key] = {
                "source": source,
                "logo": logo,
                "refer": BASE_URL,
                "timestamp": now.timestamp(),
                "tvg-id": tvg_id or "Live.Event.us",
                "sport": sport,
            }

    # Step 6: Process special events (non-sport specific)
    for channel_name, stream_url in special_events.items():
        key = f"[Live Event] {channel_name} ({TAG})"

        tvg_id, logo = leagues.get_tvg_info("Live Event", channel_name)

        events[key] = {
            "source": tokenize(stream_url, token),
            "logo": logo,
            "refer": BASE_URL,
            "timestamp": now.timestamp(),
            "tvg-id": tvg_id or "Live.Event.us",
            "sport": "Live Event",
        }

    log.info(f"Found {len(events)} events")
    return events


async def scrape() -> None:
    """Main scraping function"""
    if cached_urls := CACHE_FILE.load():
        urls.update({k: v for k, v in cached_urls.items() if v.get("source")})
        log.info(f"Loaded {len(urls)} event(s) from cache")
        return

    log.info(f'Scraping from "{BASE_URL}"')

    urls.update(await get_events())

    if new_urls := len(urls):
        log.info(f"Collected and cached {new_urls} event(s)")
    else:
        log.info("No events found")

    CACHE_FILE.write(urls)


def generate_playlists() -> None:
    """Generate VLC and TiviMate playlist files"""

    if not urls:
        log.warning("No events to generate playlists")
        return

    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    header = (
        '#EXTM3U x-tvg-url="https://epgshare01.online/'
        'epgshare01/epg_ripper_ALL_SOURCES1.xml.gz"\n'
        f"# Last Updated: {ts}\n\n"
    )

    # Filter events with sources
    valid_events = {k: v for k, v in urls.items() if v.get("source")}

    # VLC PLAYLIST
    with open(VLC_OUTPUT, "w", encoding="utf-8") as f:
        f.write(header)
        ch_no = 1

        for event_name, event_data in valid_events.items():
            url = event_data.get("source")
            logo = event_data.get(
                "logo",
                "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png",
            )
            tvg_id = event_data.get("tvg-id", "Live.Event.us")

            if not url:
                continue

            f.write(
                f'#EXTINF:-1 tvg-chno="{ch_no}" '
                f'tvg-id="{tvg_id}" '
                f'tvg-name="{event_name}" '
                f'tvg-logo="{logo}" '
                f'group-title="Live Events",{event_name}\n'
            )
            f.write(f"#EXTVLCOPT:http-referrer={REFERER}\n")
            f.write(f"#EXTVLCOPT:http-origin={ORIGIN}\n")
            f.write(f"#EXTVLCOPT:http-user-agent={USER_AGENT}\n")
            f.write(f"{url}\n\n")
            ch_no += 1

    log.info(f"Generated VLC playlist: {VLC_OUTPUT} with {ch_no - 1} streams")

    # TIVIMATE PLAYLIST
    ua_enc = quote(TIVIMATE_USER_AGENT, safe="")

    with open(TIVIMATE_OUTPUT, "w", encoding="utf-8") as f:
        f.write(header)
        ch_no = 1

        for event_name, event_data in valid_events.items():
            url = event_data.get("source")
            logo = event_data.get(
                "logo",
                "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png",
            )
            tvg_id = event_data.get("tvg-id", "Live.Event.us")

            if not url:
                continue

            f.write(
                f'#EXTINF:-1 tvg-chno="{ch_no}" '
                f'tvg-id="{tvg_id}" '
                f'tvg-name="{event_name}" '
                f'tvg-logo="{logo}" '
                f'group-title="Live Events",{event_name}\n'
            )
            f.write(
                f'{url}'
                f'|referer={REFERER}'
                f'|origin={ORIGIN}'
                f'|user-agent={ua_enc}\n\n'
            )
            ch_no += 1

    log.info(f"Generated TiviMate playlist: {TIVIMATE_OUTPUT} with {ch_no - 1} streams")


async def main() -> None:
    """Run updater and generate playlists"""
    log.info("Starting XYZ playlist generator")
    await scrape()
    generate_playlists()
    log.info("Playlist generation completed")

    print("\nPlaylists generated successfully!")
    print(f"VLC: {VLC_OUTPUT}")
    print(f"TiviMate: {TIVIMATE_OUTPUT}")
    print(f"Total streams: {len(urls)}")


if __name__ == "__main__":
    asyncio.run(main())
