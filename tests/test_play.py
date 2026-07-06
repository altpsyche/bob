"""Music plugin (plugins/play/tool.py): cross-platform open (no os.startfile) + docker-free YouTube
resolve (works without SearXNG/Docker)."""
import sys
import unittest
from pathlib import Path
from unittest import mock

import _common  # noqa: F401 — puts scripts/ + scripts/tools on sys.path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins" / "play"))
import tool  # noqa: E402
import osenv  # noqa: E402
import requests  # noqa: E402


class TestPlay(unittest.TestCase):
    def setUp(self):
        tool.configure({})

    def _resp(self, text):
        r = mock.Mock()
        r.raise_for_status = lambda: None
        r.text = text
        return r

    def test_open_uses_the_os_seam_not_startfile(self):
        opened = []
        with mock.patch.object(osenv, "open_url", lambda u: opened.append(u) or True):
            self.assertTrue(tool._open("spotify:search:x"))
        self.assertEqual(opened, ["spotify:search:x"])   # cross-platform, no os.startfile

    def test_youtube_first_video_parses_videoid(self):
        with mock.patch.object(requests, "get",
                               return_value=self._resp('junk "videoId":"abc123DEF45" junk')):
            url = tool._youtube_first_video("q")
        self.assertEqual(url, "https://www.youtube.com/watch?v=abc123DEF45")

    def test_music_play_direct_plays_without_docker(self):
        # SearXNG down (no docker): music_play must still resolve a direct video via the scrape.
        opened = []
        with mock.patch.object(osenv, "is_port_in_use", return_value=False), \
             mock.patch.object(osenv, "open_url", lambda u: opened.append(u) or True), \
             mock.patch.object(requests, "get", return_value=self._resp('"videoId":"abc123DEF45"')):
            out = tool._music_play("Arctic Monkeys", "youtube")
        self.assertIn("Playing on YouTube", out)
        self.assertTrue(opened and "watch?v=abc123DEF45" in opened[0])

    def test_music_play_falls_back_to_search_page(self):
        # Nothing resolves (no SearXNG, scrape finds no id) → open the search page, no crash.
        opened = []
        with mock.patch.object(osenv, "is_port_in_use", return_value=False), \
             mock.patch.object(osenv, "open_url", lambda u: opened.append(u) or True), \
             mock.patch.object(requests, "get", return_value=self._resp("no ids here")):
            out = tool._music_play("obscure thing", "youtube")
        self.assertIn("search", out.lower())
        self.assertTrue(opened and "search" in opened[0])


if __name__ == "__main__":
    unittest.main()
