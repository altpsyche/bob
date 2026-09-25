"""Music plugin (plugins/play): logic in invoke.py (cross-platform open, no os.startfile, docker-free
YouTube resolve), play.py only exposes it, and main() is the `bob play` CLI."""
import sys
import unittest
from pathlib import Path
from unittest import mock

import _common  # noqa: F401 — puts scripts/ + scripts/tools on sys.path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from plugins.play import invoke as play  # noqa: E402
import osenv  # noqa: E402
import requests  # noqa: E402


class TestPlay(unittest.TestCase):
    def setUp(self):
        play.configure({})
        # Default: no yt-dlp on the box, so the scrape/fallback paths are exercised deterministically
        # regardless of whether the test host happens to have yt-dlp installed (keeps tests hermetic).
        self._ytdlp = mock.patch.object(play, "_ytdlp_bin", return_value=None)
        self._ytdlp.start()
        self.addCleanup(self._ytdlp.stop)
        play._players.clear()               # isolate the session player list between tests
        self.addCleanup(play._players.clear)

    def _resp(self, text):
        r = mock.Mock()
        r.raise_for_status = lambda: None
        r.text = text
        return r

    def test_open_uses_the_os_seam_not_startfile(self):
        opened = []
        with mock.patch.object(osenv, "open_url", lambda u: opened.append(u) or True):
            self.assertTrue(play._open("spotify:search:x"))
        self.assertEqual(opened, ["spotify:search:x"])   # cross-platform, no os.startfile

    def test_youtube_first_video_parses_videoid(self):
        with mock.patch.object(requests, "get",
                               return_value=self._resp('junk "videoId":"abc123DEF45" junk')):
            url = play._youtube_first_video("q")
        self.assertEqual(url, "https://www.youtube.com/watch?v=abc123DEF45")

    def test_music_play_direct_plays_without_docker(self):
        # SearXNG down (no docker): music_play must still resolve a direct video via the scrape.
        opened = []
        with mock.patch.object(osenv, "is_port_in_use", return_value=False), \
             mock.patch.object(osenv, "open_url", lambda u: opened.append(u) or True), \
             mock.patch.object(requests, "get", return_value=self._resp('"videoId":"abc123DEF45"')):
            out = play.music_play("Arctic Monkeys", "youtube")
        self.assertIn("Playing on YouTube", out)
        self.assertTrue(opened and "watch?v=abc123DEF45" in opened[0])

    def test_ytdlp_search_resolves_watch_url(self):
        # The preferred, stable resolver: yt-dlp prints the first search hit's id (no HTML scraping).
        with mock.patch.object(play, "_ytdlp_bin", return_value="yt-dlp"), \
             mock.patch("subprocess.run",
                        return_value=mock.Mock(stdout="abc123DEF45\n", returncode=0)):
            url = play._ytdlp_search("Arctic Monkeys")
        self.assertEqual(url, "https://www.youtube.com/watch?v=abc123DEF45")

    def test_ytdlp_absent_returns_none(self):
        with mock.patch.object(play, "_ytdlp_bin", return_value=None):
            self.assertIsNone(play._ytdlp_search("q"))

    def test_music_play_prefers_ytdlp_over_scrape(self):
        # When yt-dlp resolves, the fragile HTML scrape (requests.get) must NOT be reached.
        # (_play_stream forced off so the deterministic browser-fallback path is exercised.)
        opened = []
        with mock.patch.object(osenv, "is_port_in_use", return_value=False), \
             mock.patch.object(osenv, "open_url", lambda u: opened.append(u) or True), \
             mock.patch.object(play, "_ytdlp_bin", return_value="yt-dlp"), \
             mock.patch.object(play, "_play_stream", return_value=False), \
             mock.patch("subprocess.run",
                        return_value=mock.Mock(stdout="abc123DEF45\n", returncode=0)), \
             mock.patch.object(requests, "get", side_effect=AssertionError("scrape must not run")):
            out = play.music_play("Arctic Monkeys", "youtube")
        self.assertIn("Playing on YouTube", out)
        self.assertTrue(opened and "watch?v=abc123DEF45" in opened[0])

    def test_play_stream_uses_mpv_detached_visible(self):
        # The real fix: start the song via mpv in a VISIBLE window, detached so it outlives the turn.
        with mock.patch.object(play, "_ytdlp_bin", return_value="/venv/bin/yt-dlp"), \
             mock.patch("shutil.which", return_value="/usr/bin/mpv"), \
             mock.patch.object(play, "_mpv_supports", return_value=True), \
             mock.patch("subprocess.Popen", return_value=mock.Mock(pid=4242)) as popen:
            ok = play._play_stream("https://www.youtube.com/watch?v=abc123DEF45")
        self.assertTrue(ok)
        argv = popen.call_args[0][0]
        self.assertEqual(argv[0], "/usr/bin/mpv")
        self.assertIn("--force-window=yes", argv)          # a visible player window
        self.assertNotIn("--no-video", argv)               # not headless anymore
        self.assertIn("--focus-on=never", argv)            # visible but doesn't grab terminal focus
        self.assertIn("https://www.youtube.com/watch?v=abc123DEF45", argv)
        # mpv is pointed at OUR yt-dlp (it only searches PATH otherwise, and ours is in the venv)
        self.assertIn("--script-opts=ytdl_hook-ytdl_path=/venv/bin/yt-dlp", argv)
        self.assertTrue(popen.call_args.kwargs.get("start_new_session"))   # detached
        self.assertIn(4242, play._players)                 # tracked so music_stop can reap it

    def test_play_stream_omits_focus_flag_on_old_mpv(self):
        # An mpv too old for --focus-on must not get the flag (mpv errors on unknown options -> no play).
        with mock.patch.object(play, "_ytdlp_bin", return_value="/venv/bin/yt-dlp"), \
             mock.patch("shutil.which", return_value="/usr/bin/mpv"), \
             mock.patch.object(play, "_mpv_supports", return_value=False), \
             mock.patch("subprocess.Popen", return_value=mock.Mock(pid=7)) as popen:
            self.assertTrue(play._play_stream("http://x"))
        self.assertNotIn("--focus-on=never", popen.call_args[0][0])

    def test_music_stop_reaps_tracked_players(self):
        import osenv
        play._players.extend([111, 222])
        reaped = []
        with mock.patch.object(osenv, "pid_alive", return_value=True), \
             mock.patch.object(osenv, "stop_process_tree", side_effect=lambda p: reaped.append(p)):
            out = play.music_stop()
        self.assertIn("Stopped the music", out)
        self.assertEqual(sorted(reaped), [111, 222])
        self.assertEqual(play._players, [])                # cleared after stopping

    def test_music_stop_when_nothing_playing(self):
        self.assertIn("No music is playing", play.music_stop())

    def test_new_play_reaps_previous_player(self):
        # Playing again must not stack windows/audio: the prior mpv is reaped before the new one starts.
        import osenv
        play._players.append(999)
        reaped = []
        with mock.patch.object(play, "_ytdlp_bin", return_value="/venv/bin/yt-dlp"), \
             mock.patch("shutil.which", return_value="/usr/bin/mpv"), \
             mock.patch.object(play, "_mpv_supports", return_value=False), \
             mock.patch.object(osenv, "pid_alive", return_value=True), \
             mock.patch.object(osenv, "stop_process_tree", side_effect=lambda p: reaped.append(p)), \
             mock.patch("subprocess.Popen", return_value=mock.Mock(pid=1234)):
            ok = play._play_stream("http://x")
        self.assertTrue(ok)
        self.assertEqual(reaped, [999])                    # old player reaped
        self.assertEqual(play._players, [1234])            # only the new one tracked

    def test_play_stream_false_without_mpv_or_ytdlp(self):
        with mock.patch.object(play, "_ytdlp_bin", return_value="yt-dlp"), \
             mock.patch("shutil.which", return_value=None):
            self.assertFalse(play._play_stream("http://x"))   # no mpv
        with mock.patch.object(play, "_ytdlp_bin", return_value=None), \
             mock.patch("shutil.which", return_value="/usr/bin/mpv"):
            self.assertFalse(play._play_stream("http://x"))   # no yt-dlp for mpv to stream through

    def test_music_play_streams_via_player_without_browser(self):
        # When a local player plays, the song starts directly -- no browser tab is opened.
        with mock.patch.object(osenv, "is_port_in_use", return_value=False), \
             mock.patch.object(play, "_ytdlp_bin", return_value="yt-dlp"), \
             mock.patch("subprocess.run",
                        return_value=mock.Mock(stdout="abc123DEF45\n", returncode=0)), \
             mock.patch.object(play, "_play_stream", return_value=True), \
             mock.patch.object(osenv, "open_url",
                               side_effect=AssertionError("no browser when the player plays")):
            out = play.music_play("Arctic Monkeys", "youtube")
        self.assertIn("Playing on YouTube", out)

    def test_music_play_falls_back_to_search_page(self):
        # Nothing resolves (no SearXNG, scrape finds no id) → open the search page, no crash.
        opened = []
        with mock.patch.object(osenv, "is_port_in_use", return_value=False), \
             mock.patch.object(osenv, "open_url", lambda u: opened.append(u) or True), \
             mock.patch.object(requests, "get", return_value=self._resp("no ids here")):
            out = play.music_play("obscure thing", "youtube")
        self.assertIn("search", out.lower())
        self.assertTrue(opened and "search" in opened[0])


class TestPlayLayout(unittest.TestCase):
    """Logic lives in invoke.py; tool.py only exposes it; the plugin is Python only."""

    def _tool(self):
        import importlib.util
        path = Path(__file__).resolve().parent.parent / "plugins" / "play" / "tool.py"
        spec = importlib.util.spec_from_file_location("bob_tool_play_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_tool_dispatches_to_invoke_functions(self):
        mod = self._tool()
        self.assertIs(mod.DISPATCH["music_play"], play.music_play)
        self.assertIs(mod.DISPATCH["music_stop"], play.music_stop)

    def test_play_tools_declared_mutating(self):
        # Launching or killing a player changes machine state: never treated as a read.
        self.assertEqual(self._tool().MUTATING_TOOLS, {"music_play", "music_stop"})

    def test_no_powershell_entry_point(self):
        d = Path(__file__).resolve().parent.parent / "plugins" / "play"
        self.assertFalse((d / "invoke.ps1").exists())

    def test_main_routes_platform_flag(self):
        seen = {}
        with mock.patch.object(play, "music_play", side_effect=lambda q, p: seen.update(q=q, p=p) or "ok"), \
             mock.patch("bob_core.load_config", return_value={}), mock.patch("sys.stdout"):
            rc = play.main(["--youtube", "arctic", "monkeys"])
        self.assertEqual(rc, 0)
        self.assertEqual(seen, {"q": "arctic monkeys", "p": "youtube"})

    def test_main_without_query_is_usage_error(self):
        with mock.patch("sys.stderr"):
            self.assertEqual(play.main([]), 1)


if __name__ == "__main__":
    unittest.main()
