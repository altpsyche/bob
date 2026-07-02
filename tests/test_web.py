"""M13 — web_fetch SSRF guard (M9)."""
import socket
import unittest

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
        # NE0: a public URL that 302-redirects to a private host must be blocked at the second hop.
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


if __name__ == "__main__":
    unittest.main()
