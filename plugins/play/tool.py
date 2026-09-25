"""Bob plugin tool: music_play / music_stop, the agent-facing side of `bob play`.

The logic lives in plugins/play/invoke.py; this module only exposes it to the agent."""
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO))

from plugins.play import invoke  # noqa: E402
from plugins.play.invoke import music_play, music_stop  # noqa: E402


def configure(config: dict) -> None:
    invoke.configure(config)


def test() -> str:
    url = invoke._find_youtube_url("Arctic Monkeys")
    searxng_status = f"SearXNG found: {url}" if url else "SearXNG unavailable (fallback active)"
    platform = "Spotify" if invoke._spotify_installed() else "YouTube"
    return f"music_play: OK. default platform: {platform} | {searxng_status} (CLI: bob play <query>)"


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

DISPATCH = {"music_play": music_play, "music_stop": music_stop}

# Both change machine state (launch or kill a player process, open a URL handler).
MUTATING_TOOLS = {"music_play", "music_stop"}

# Only music_play leaves voice mode (so the song can play without the mic transcribing the lyrics);
# music_stop stays in voice so you can stop the music and keep talking.
EXIT_VOICE = {"music_play"}
