"""Bob tool: music_play — open music in Spotify or YouTube Music.

Voice-safe: fire-and-forget, no confirmation prompt, no blocking.

YouTube path: resolves a direct youtube.com/watch URL (SearXNG if up -> yt-dlp
-> HTML scrape -> search page), then STARTS it: streams audio via mpv when
available (autoplays immediately and outlives the voice turn), else opens the
browser with autoplay hinted.

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
    root = Path(__file__).parent.parent.parent
    for p in (root / "scripts", root / "scripts" / "tools"):   # bob_core._port + osenv on sys.path
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))


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
    """First choice: ask SearXNG (only if it's already listening — no docker needed) for a direct
    youtube.com/watch result."""
    try:
        import osenv
        import requests
        from bob_core import _port  # N7 — single source of truth for ports
        port = _port(_cfg, "searxngPort")
        if not osenv.is_port_in_use(port):
            return None                    # SearXNG not up; the direct scrape below handles it
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
        print(f"[play] youtube lookup via SearXNG failed: {e}", file=sys.stderr)
    return None


def _ytdlp_bin() -> str | None:
    """A yt-dlp binary if one is available: PATH first, then the venv-litellm one (installed with Bob's
    deps), then a repo-staged bin/yt-dlp. Preferred over the HTML scrape because it resolves a search to
    a video id through a maintained API, not markup that can change out from under us -- and mpv needs
    it to stream YouTube."""
    import shutil
    exe = shutil.which("yt-dlp")
    if exe:
        return exe
    try:
        import osenv
        for cand in (osenv.venv_exe("venv-litellm", "yt-dlp"), osenv.bin_exe("yt-dlp")):
            if cand.exists():
                return str(cand)
    except Exception:
        pass
    return None


def _ytdlp_search(query: str) -> str | None:
    """Stable resolver (preferred when yt-dlp is present): ask yt-dlp for the first search hit's id
    without downloading. No SearXNG, no API key, no HTML scraping. None if yt-dlp is absent/fails."""
    exe = _ytdlp_bin()
    if not exe:
        return None
    try:
        import re
        import subprocess
        r = subprocess.run(
            [exe, "--no-warnings", "--flat-playlist", "--print", "id", f"ytsearch1:{query}"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        for line in r.stdout.splitlines():
            line = line.strip()
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", line):
                return f"https://www.youtube.com/watch?v={line}"
    except Exception as e:
        print(f"[play] yt-dlp lookup failed: {e}", file=sys.stderr)
    return None


def _youtube_first_video(query: str) -> str | None:
    """Last-resort docker-free lookup: fetch YouTube's results page and pull the first videoId from the
    embedded JSON. No SearXNG, no API key. FRAGILE by nature -- it depends on YouTube's page markup, so
    a layout change can silently stop it matching; that's why _ytdlp_search is preferred when yt-dlp is
    available, and why the search-page fallback below always exists as a backstop."""
    try:
        import re
        import requests
        r = requests.get(
            "https://www.youtube.com/results",
            params={"search_query": query},
            headers={"User-Agent": "Mozilla/5.0 (compatible; Bob/1.0)"},
            timeout=8,
        )
        r.raise_for_status()
        m = re.search(r'"videoId":"([A-Za-z0-9_-]{11})"', r.text)   # markup dependency (see docstring)
        if m:
            return f"https://www.youtube.com/watch?v={m.group(1)}"
    except Exception as e:
        print(f"[play] youtube direct lookup failed: {e}", file=sys.stderr)
    return None


def _play_stream(url: str) -> bool:
    """Actually START the song: stream `url` through mpv (audio-only), detached so it keeps playing
    after voice/the shell moves on. mpv autoplays immediately -- a browser tab does NOT (browsers block
    autoplay-with-sound until a click), which is why opening the watch page 'just goes to the page'
    without playing. mpv streams via yt-dlp, so both must be present; returns False otherwise so the
    caller falls back to opening the URL in a browser."""
    import shutil
    mpv = shutil.which("mpv")
    ytdlp = _ytdlp_bin()
    if not (mpv and ytdlp):
        return False
    import subprocess
    try:
        subprocess.Popen(
            # Point mpv at OUR yt-dlp explicitly: mpv's ytdl hook only searches PATH, and Bob's yt-dlp
            # lives in the venv (off PATH). Without this, mpv can't resolve the stream and nothing plays.
            [mpv, "--no-video", "--no-terminal",
             f"--script-opts=ytdl_hook-ytdl_path={ytdlp}", url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            start_new_session=True,   # detach: the song outlives the voice turn / shell command
        )
        return True
    except OSError as e:
        print(f"[play] mpv playback failed: {e}", file=sys.stderr)
        return False


def _autoplay_url(url: str) -> str:
    """Browser fallback: hint autoplay on the watch URL (best-effort -- a browser may still gate
    autoplay-with-sound until the user clicks)."""
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}autoplay=1"


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
        # Resolve a direct watch URL, most-robust first: SearXNG if it's up, then yt-dlp if installed
        # (stable API), then the fragile HTML scrape as a backstop.
        url = _find_youtube_url(query) or _ytdlp_search(query) or _youtube_first_video(query)
        if url:
            # Start the song for real via a local player; if none, open the browser (autoplay hinted).
            if _play_stream(url):
                return f"Playing on YouTube: {query}"
            return _launch(_autoplay_url(url), f"Playing on YouTube: {query}")
        # Couldn't resolve a video: open the search page.
        return _launch(f"https://music.youtube.com/search?q={q}",
                       f"Opening YouTube search for {query} (couldn't resolve a direct video)")

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
