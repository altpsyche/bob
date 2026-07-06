"""Bob tool: music_play — open music in Spotify or YouTube Music.

Voice-safe: fire-and-forget, no confirmation prompt, no blocking.

YouTube path: searches SearXNG for a direct youtube.com/watch URL and opens
it — video starts playing immediately. Falls back to YouTube Music search page
if SearXNG is unavailable.

Spotify path: opens spotify:search: URI (Spotify handles playback).
"""
import os
import sys
import urllib.parse
from pathlib import Path

_cfg: dict = {}


def configure(config: dict) -> None:
    global _cfg
    _cfg = config
    scripts_dir = str(Path(__file__).parent.parent.parent / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)  # N7 — allow bob_core._port import


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _spotify_installed() -> bool:
    """Cross-platform best-effort: a `spotify` launcher on PATH (Linux native/flatpak wrapper, macOS
    CLI), the macOS app bundle, or the Windows install locations."""
    import shutil
    if shutil.which("spotify"):
        return True
    appdata = os.environ.get("APPDATA", "")
    localappdata = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        Path(appdata) / "Spotify" / "Spotify.exe",
        Path(localappdata) / "Microsoft" / "WindowsApps" / "Spotify.exe",
        Path("/Applications/Spotify.app"),
    ]
    return any(p.exists() for p in candidates)


def _find_youtube_url(query: str) -> str | None:
    """Ask SearXNG for the first youtube.com/watch result for query."""
    try:
        import requests
        from bob_core import _port  # N7 — single source of truth for ports
        port = _port(_cfg, "searxngPort")
        r = requests.get(
            f"http://localhost:{port}/search",
            params={"q": f"{query} site:youtube.com", "format": "json", "pageno": 1},
            timeout=5,
        )
        r.raise_for_status()
        for result in r.json().get("results", []):
            url = result.get("url", "")
            if "youtube.com/watch" in url:
                return url
    except Exception as e:
        # M16 — SearXNG lookup is best-effort (caller falls back to the search page),
        # but log the swallow so a persistently-broken SearXNG isn't invisible.
        print(f"[play] youtube lookup via SearXNG failed: {e}", file=sys.stderr)
    return None


def _open(uri: str) -> bool:
    """Open an http(s):// URL or a scheme URI (e.g. spotify:) via the OS seam — cross-platform
    (xdg-open / open / webbrowser), never the Windows-only os.startfile. False if no opener fired."""
    import osenv   # scripts/ is on sys.path via configure()
    return osenv.open_url(uri)


# ---------------------------------------------------------------------------
# Tool function
# ---------------------------------------------------------------------------

def _launch(uri: str, ok_msg: str) -> str:
    """Open `uri`; report success or a clear 'no opener' message (headless box / no URL handler
    registered) instead of pretending it played."""
    if _open(uri):
        return ok_msg
    return f"Couldn't open a player (no URL handler, or a headless session). URL: {uri}"


def _music_play(query: str, platform: str = "auto") -> str:
    query = query.strip()
    if not query:
        return "No query provided."

    platform = platform.lower()
    q = urllib.parse.quote(query)

    if platform == "spotify":
        return _launch(f"spotify:search:{q}", f"Opening Spotify: {query}")

    if platform in ("youtube", "auto") and not (platform == "auto" and _spotify_installed()):
        # Try to find a direct watch URL so the video plays immediately (needs SearXNG).
        url = _find_youtube_url(query)
        if url:
            return _launch(url, f"Playing on YouTube: {query}")
        # SearXNG unavailable: open the search page (start SearXNG with 'bob services start' for
        # instant play).
        return _launch(f"https://music.youtube.com/search?q={q}",
                       f"Opening YouTube search for {query} (SearXNG down; showing the search page)")

    # auto + Spotify installed
    return _launch(f"spotify:search:{q}", f"Opening Spotify: {query}")


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

def test() -> str:
    url = _find_youtube_url("Arctic Monkeys")
    searxng_status = f"SearXNG found: {url}" if url else "SearXNG unavailable (fallback active)"
    platform = "Spotify" if _spotify_installed() else "YouTube"
    return f"music_play: OK. default platform: {platform} | {searxng_status}"


# ---------------------------------------------------------------------------
# Schema + dispatch
# ---------------------------------------------------------------------------

TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "music_play",
            "description": (
                "Open music in Spotify or YouTube. "
                "Use when the user asks to play a song, artist, album, or playlist. "
                "Finds a direct YouTube video URL so music starts playing immediately. "
                "Prefers Spotify if installed. "
                "Pass platform='youtube' if the user says 'on YouTube'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Artist, song, album, or playlist to search for. "
                            "Examples: 'Arctic Monkeys', 'Bohemian Rhapsody', "
                            "'lofi hip hop', 'dark side of the moon'."
                        ),
                    },
                    "platform": {
                        "type": "string",
                        "enum": ["auto", "spotify", "youtube"],
                        "description": (
                            "'auto' tries Spotify first, falls back to YouTube. "
                            "'spotify' forces Spotify. 'youtube' forces YouTube."
                        ),
                    },
                },
                "required": ["query"],
            },
        },
    }
]

DISPATCH = {"music_play": _music_play}

EXIT_VOICE = True
