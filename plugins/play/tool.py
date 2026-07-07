"""Bob tool: music_play — open music in Spotify or YouTube Music.

Voice-safe: fire-and-forget, no confirmation prompt, no blocking.

YouTube path: resolves a direct youtube.com/watch URL (SearXNG if up -> yt-dlp
-> HTML scrape -> search page), then STARTS it: streams via mpv in a visible
window when available (autoplays immediately, outlives the voice turn, and can
be stopped with music_stop), else opens the browser with autoplay hinted.

Spotify path: opens spotify:search: URI (Spotify handles playback).
"""
import os
import sys
import urllib.parse
from pathlib import Path

_cfg: dict = {}
_players: list = []   # PIDs of mpv players we launched this session (so music_stop can reap them)
_mpv_caps: dict = {}  # mpv path -> set of supported --options (probed once; older mpv lacks --focus-on)


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


def _stop_players() -> int:
    """Reap every mpv we launched this session. Returns how many were stopped. start_new_session made
    each a process-group leader, so osenv.stop_process_tree reaps mpv and its yt-dlp child."""
    import osenv
    n = 0
    for pid in _players:
        try:
            if osenv.pid_alive(pid):
                osenv.stop_process_tree(pid)
                n += 1
        except Exception:   # noqa: BLE001 — best-effort teardown
            pass
    _players.clear()
    return n


def _mpv_supports(mpv: str, option: str) -> bool:
    """True if this mpv build accepts --<option>. Probed once per binary (mpv errors out on an unknown
    option, which would kill playback), so we can pass newer flags only where supported."""
    caps = _mpv_caps.get(mpv)
    if caps is None:
        import subprocess
        try:
            out = subprocess.run([mpv, "--list-options"], capture_output=True, text=True, timeout=5).stdout
        except Exception:   # noqa: BLE001 — probe is best-effort
            out = ""
        caps = {ln.split()[0] for ln in out.splitlines() if ln.strip().startswith("--")}
        _mpv_caps[mpv] = caps
    return f"--{option}" in caps


def _play_stream(url: str) -> bool:
    """Actually START the song: stream `url` through mpv in a VISIBLE window (so you can see what's
    playing, pause, and close it — and its own audio doesn't need the mic for control), detached so it
    keeps playing after the voice turn ends. mpv autoplays immediately -- a browser tab does NOT
    (browsers block autoplay-with-sound until a click), which is why opening the watch page 'just goes
    to the page' without playing. mpv streams via yt-dlp, so both must be present; returns False
    otherwise so the caller falls back to opening the URL in a browser."""
    import shutil
    mpv = shutil.which("mpv")
    ytdlp = _ytdlp_bin()
    if not (mpv and ytdlp):
        return False
    import subprocess
    _stop_players()   # one player at a time — don't stack windows/audio on a new request
    # Point mpv at OUR yt-dlp explicitly: mpv's ytdl hook only searches PATH, and Bob's yt-dlp lives in
    # the venv (off PATH). Without this, mpv can't resolve the stream and nothing plays.
    # --force-window shows a window even before video decodes, so there's always a visible player.
    argv = [mpv, "--force-window=yes", "--no-terminal",
            f"--script-opts=ytdl_hook-ytdl_path={ytdlp}"]
    if _mpv_supports(mpv, "focus-on"):
        # Don't steal focus from the terminal when the window opens (mpv >= 0.38) -- so the player is
        # visible but doesn't "take over": you keep typing in your shell.
        argv.append("--focus-on=never")
    argv.append(url)
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            start_new_session=True,   # detach + own process group: the song outlives the voice turn
        )
        _players.append(proc.pid)
        return True
    except OSError as e:
        print(f"[play] mpv playback failed: {e}", file=sys.stderr)
        return False


def _music_stop() -> str:
    """Stop music that music_play started (closes the mpv window)."""
    return "Stopped the music." if _stop_players() else "No music is playing."


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
                return f"Playing on YouTube: {query} (in a player window; say 'stop the music' to end it)"
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
    },
    {
        "type": "function",
        "function": {
            "name": "music_stop",
            "description": (
                "Stop music that music_play started (closes the player window). "
                "Use when the user asks to stop, pause, or turn off the music."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

DISPATCH = {"music_play": _music_play, "music_stop": _music_stop}

# Only music_play leaves voice mode (so the song can play without the mic transcribing the lyrics);
# music_stop stays in voice so you can stop the music and keep talking.
EXIT_VOICE = {"music_play"}
