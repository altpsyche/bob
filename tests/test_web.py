"""web_fetch SSRF guard; non-docker web_search fallback."""
import socket
import unittest
from unittest import mock

import _common
import web


class TestWebFetchGuard(unittest.TestCase):
    def setUp(self):
        web.configure(_common.fake_config())
        self._orig = socket.getaddrinfo

    def tearDown(self):
        socket.getaddrinfo = self._orig
        web._allow_private_fetch = False

    def _fake_resolve(self, ip):
        socket.getaddrinfo = lambda host, *a, **k: [(2, 1, 6, "", (ip, 0))]

    def test_blocks_loopback(self):
        self._fake_resolve("127.0.0.1")
        self.assertTrue(web._is_blocked_host("whatever"))

    def test_blocks_private(self):
        self._fake_resolve("10.1.2.3")
        self.assertTrue(web._is_blocked_host("whatever"))

    def test_allows_public(self):
        self._fake_resolve("93.184.216.34")  # example.com
        self.assertFalse(web._is_blocked_host("example.com"))

    def test_fetch_rejects_non_http_scheme(self):
        out = web._web_fetch("file:///etc/passwd")
        self.assertIn("blocked scheme", out)

    def test_fetch_rejects_private_host(self):
        self._fake_resolve("192.168.0.5")
        out = web._web_fetch("http://intranet.local/secret")
        self.assertIn("blocked host", out)

    def test_fetch_blocks_redirect_to_private(self):
        # a public URL that 302-redirects to a private host must be blocked at the second hop.
        socket.getaddrinfo = lambda host, *a, **k: [
            (2, 1, 6, "", (("93.184.216.34" if host == "public.example" else "10.0.0.5"), 0))
        ]

        class _Redirect:
            is_redirect = True
            headers = {"Location": "http://internal.local/secret"}

        orig_get = web.requests.get
        web.requests.get = lambda *a, **k: _Redirect()
        try:
            out = web._web_fetch("http://public.example/page")
        finally:
            web.requests.get = orig_get
        self.assertIn("blocked host", out)
        self.assertIn("internal.local", out)

    def test_opt_in_allows_private(self):
        web.configure(_common.fake_config(agent={"allowPrivateFetch": True}))
        self._fake_resolve("127.0.0.1")
        # host guard is bypassed; the call will fail on the network, not the guard
        out = web._web_fetch("http://127.0.0.1:9/nothing")
        self.assertNotIn("blocked host", out)


_DDG_HTML = (
    '<a class="result__a" rel="nofollow" '
    'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&rut=x">Example &amp; Title</a>'
    '<a class="result__snippet" href="x">A useful &amp; short snippet</a>'
)


class TestWebSearchProviders(unittest.TestCase):
    """web_search is a cross-OS provider abstraction: `ddgs` is the keyless default (no Docker, no
    daemon); brave/tavily/searxng are opt-in; any selected provider falls back to ddgs, then to a
    last-ditch stdlib scrape. So search behaves identically on every OS and never hard-fails."""

    def _resp(self, *, json_data=None, text=None):
        r = mock.Mock()
        r.raise_for_status = lambda: None
        if json_data is not None:
            r.json = lambda: json_data
        if text is not None:
            r.text = text
        return r

    def test_ddgs_is_the_default_provider(self):
        web.configure(_common.fake_config())              # default searchProvider = ddgs
        with mock.patch.object(web, "_ddgs_search", return_value="- T\n  http://u\n  c") as d:
            out = web._web_search("q")
        d.assert_called_once()
        self.assertIn("http://u", out)

    def test_selected_provider_falls_back_to_ddgs(self):
        # brave selected but its key/call fails -> the keyless ddgs default still answers.
        web.configure(_common.fake_config(agent={"searchProvider": "brave"}))
        with mock.patch.object(web, "_brave_search", side_effect=Exception("no key")), \
             mock.patch.object(web, "_ddgs_search", return_value="- T\n  http://ddg\n  c"):
            out = web._web_search("q")
        self.assertIn("http://ddg", out)

    def test_searxng_provider_queries_the_service(self):
        web.configure(_common.fake_config(agent={"searchProvider": "searxng"}))
        with mock.patch.object(web, "_ensure_searxng", return_value=""), \
             mock.patch.object(web.requests, "get",
                               return_value=self._resp(json_data={"results": [
                                   {"title": "T", "url": "http://u", "content": "c"}]})):
            out = web._web_search("q")
        self.assertIn("http://u", out)

    def test_last_ditch_scrape_when_ddgs_unavailable(self):
        # ddgs lib missing -> the thin stdlib DuckDuckGo scrape still returns results.
        web.configure(_common.fake_config())
        with mock.patch.object(web, "_ddgs_search", return_value=None), \
             mock.patch.object(web.requests, "post", return_value=self._resp(text=_DDG_HTML)):
            out = web._web_search("q")
        self.assertIn("https://example.com/page", out)   # DDG redirect decoded to the real URL
        self.assertIn("Example & Title", out)            # HTML entities unescaped, tags stripped
        self.assertIn("useful & short snippet", out)

    def test_reports_unavailable_when_everything_fails(self):
        web.configure(_common.fake_config())
        with mock.patch.object(web, "_ddgs_search", return_value=None), \
             mock.patch.object(web, "_ddg_scrape_fallback", return_value=None):
            out = web._web_search("q")
        self.assertIn("web_search unavailable", out)


if __name__ == "__main__":
    unittest.main()
