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
        # Default: no yt-dlp on the box, so the scrape/fallback paths are exercised deterministically
        # regardless of whether the test host happens to have yt-dlp installed (keeps tests hermetic).
        self._ytdlp = mock.patch.object(tool, "_ytdlp_bin", return_value=None)
        self._ytdlp.start()
        self.addCleanup(self._ytdlp.stop)

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

    def test_ytdlp_search_resolves_watch_url(self):
        # The preferred, stable resolver: yt-dlp prints the first search hit's id (no HTML scraping).
        with mock.patch.object(tool, "_ytdlp_bin", return_value="yt-dlp"), \
             mock.patch("subprocess.run",
                        return_value=mock.Mock(stdout="abc123DEF45\n", returncode=0)):
            url = tool._ytdlp_search("Arctic Monkeys")
        self.assertEqual(url, "https://www.youtube.com/watch?v=abc123DEF45")

    def test_ytdlp_absent_returns_none(self):
        with mock.patch.object(tool, "_ytdlp_bin", return_value=None):
            self.assertIsNone(tool._ytdlp_search("q"))

    def test_music_play_prefers_ytdlp_over_scrape(self):
        # When yt-dlp resolves, the fragile HTML scrape (requests.get) must NOT be reached.
        # (_play_stream forced off so the deterministic browser-fallback path is exercised.)
        opened = []
        with mock.patch.object(osenv, "is_port_in_use", return_value=False), \
             mock.patch.object(osenv, "open_url", lambda u: opened.append(u) or True), \
             mock.patch.object(tool, "_ytdlp_bin", return_value="yt-dlp"), \
             mock.patch.object(tool, "_play_stream", return_value=False), \
             mock.patch("subprocess.run",
                        return_value=mock.Mock(stdout="abc123DEF45\n", returncode=0)), \
             mock.patch.object(requests, "get", side_effect=AssertionError("scrape must not run")):
            out = tool._music_play("Arctic Monkeys", "youtube")
        self.assertIn("Playing on YouTube", out)
        self.assertTrue(opened and "watch?v=abc123DEF45" in opened[0])

    def test_play_stream_uses_mpv_detached(self):
        # The real fix: start the song via mpv (audio-only), detached so it outlives the voice turn.
        with mock.patch.object(tool, "_ytdlp_bin", return_value="yt-dlp"), \
             mock.patch("shutil.which", return_value="/usr/bin/mpv"), \
             mock.patch("subprocess.Popen") as popen:
            ok = tool._play_stream("https://www.youtube.com/watch?v=abc123DEF45")
        self.assertTrue(ok)
        argv = popen.call_args[0][0]
        self.assertEqual(argv[0], "/usr/bin/mpv")
        self.assertIn("--no-video", argv)
        self.assertIn("https://www.youtube.com/watch?v=abc123DEF45", argv)
        self.assertTrue(popen.call_args.kwargs.get("start_new_session"))   # detached

    def test_play_stream_false_without_mpv_or_ytdlp(self):
        with mock.patch.object(tool, "_ytdlp_bin", return_value="yt-dlp"), \
             mock.patch("shutil.which", return_value=None):
            self.assertFalse(tool._play_stream("http://x"))   # no mpv
        with mock.patch.object(tool, "_ytdlp_bin", return_value=None), \
             mock.patch("shutil.which", return_value="/usr/bin/mpv"):
            self.assertFalse(tool._play_stream("http://x"))   # no yt-dlp for mpv to stream through

    def test_music_play_streams_via_player_without_browser(self):
        # When a local player plays, the song starts directly -- no browser tab is opened.
        with mock.patch.object(osenv, "is_port_in_use", return_value=False), \
             mock.patch.object(tool, "_ytdlp_bin", return_value="yt-dlp"), \
             mock.patch("subprocess.run",
                        return_value=mock.Mock(stdout="abc123DEF45\n", returncode=0)), \
             mock.patch.object(tool, "_play_stream", return_value=True), \
             mock.patch.object(osenv, "open_url",
                               side_effect=AssertionError("no browser when the player plays")):
            out = tool._music_play("Arctic Monkeys", "youtube")
        self.assertIn("Playing on YouTube", out)

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
