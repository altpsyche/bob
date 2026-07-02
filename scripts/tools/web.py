"""Bob tool: web_search (SearXNG) and web_fetch."""
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse

import requests

_cfg: dict = {}
_searxng_url: str = ""
_allow_private_fetch: bool = False  # M9 — gate SSRF-prone fetches behind an explicit flag
_MAX_REDIRECTS = 5  # NE0 — cap manual redirect following so each hop can be re-validated


def configure(config: dict) -> None:
    global _cfg, _searxng_url, _allow_private_fetch
    _cfg = config
    import sys
    from pathlib import Path
    scripts_dir = str(Path(__file__).parent.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from bob_core import _port  # N7 — single source of truth for ports
    _searxng_url = f"http://localhost:{_port(config, 'searxngPort')}/search"
    _allow_private_fetch = bool(config.get("agent", {}).get("allowPrivateFetch", False))


def _is_blocked_host(host: str) -> bool:
    """True if the host resolves to a loopback/private/link-local/reserved address (SSRF risk).
    DNS failures return False so requests raises its own clean error instead of being masked."""
    if not host:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (addr.is_loopback or addr.is_private or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return True
    return False


def _web_search(query: str, num_results: int = 5) -> str:
    if not _searxng_url:
        return "web_search not configured (SearXNG URL missing)"
    try:
        r = requests.get(
            _searxng_url,
            params={"q": query, "format": "json", "pageno": 1},
            timeout=10,
        )
        r.raise_for_status()
        results = r.json().get("results", [])[:num_results]
    except Exception as e:
        return f"web_search error: {e}\nIs SearXNG running? Try: bob services start"
    if not results:
        return "(no results)"
    return "\n\n".join(
        f"- {x['title']}\n  {x['url']}\n  {x.get('content', '')[:200]}"
        for x in results
    )


def _web_fetch(url: str) -> str:
    # M9 / NE0 — allowlist http(s) and re-validate the host on EVERY hop: the initial URL AND each
    # redirect Location. Following redirects blindly (requests' default) let a public URL 302 into a
    # loopback/private target (e.g. cloud metadata at 169.254.169.254); manual per-hop validation
    # closes that. Residual: a DNS-rebinding TOCTOU window remains between the check and the connect —
    # allowPrivateFetch stays the hard gate, and every resolved address is checked each hop.
    current = url
    hops = 0
    while True:
        parsed = urlparse(current)
        if parsed.scheme not in ("http", "https"):
            return f"web_fetch error: blocked scheme '{parsed.scheme or '(none)'}' (only http/https allowed)"
        if not _allow_private_fetch and _is_blocked_host(parsed.hostname or ""):
            return (
                f"web_fetch error: blocked host '{parsed.hostname}' "
                "(loopback/private address; set agent.allowPrivateFetch = $true to override)"
            )
        try:
            r = requests.get(
                current,
                timeout=15,
                headers={"User-Agent": "Mozilla/5.0"},
                allow_redirects=False,  # follow manually so each hop is re-validated above
            )
        except Exception as e:
            return f"web_fetch error: {e}"
        if r.is_redirect:  # 301/302/303/307/308 with a Location
            loc = r.headers.get("Location")
            if not loc:
                return "web_fetch error: redirect without a Location header"
            hops += 1
            if hops > _MAX_REDIRECTS:
                return f"web_fetch error: too many redirects (>{_MAX_REDIRECTS})"
            current = urljoin(current, loc)  # resolve relative redirects; re-checked next loop
            continue
        try:
            r.raise_for_status()
        except Exception as e:
            return f"web_fetch error: {e}"
        text = re.sub(r"<[^>]+>", " ", r.text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:4000]


def test() -> str:
    return _web_search("local LLM news 2025", num_results=2)


TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web via SearXNG (private, local search engine)",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "num_results": {
                        "type": "integer",
                        "description": "Number of results to return (default 5)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch and read the text content of a URL (HTML stripped)",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"},
                },
                "required": ["url"],
            },
        },
    },
]

DISPATCH = {
    "web_search": _web_search,
    "web_fetch": _web_fetch,
}
