"""Bob tool: web_search (a cross-OS provider abstraction) and web_fetch.

Web search has ONE default that behaves identically on every OS with no Docker and no daemon: `ddgs`
(Dux Distributed Global Search), a pure-Python in-process metasearch library aggregating DuckDuckGo /
Bing / Google. Optional providers selected via `agent.searchProvider` + a key: `brave`, `tavily`, and
`searxng` (the opt-in Docker service). Any selected provider falls back to `ddgs` (then a last-ditch
stdlib scrape), so search never hard-fails."""
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse

import requests

_cfg: dict = {}
_searxng_url: str = ""
_allow_private_fetch: bool = False  # gate SSRF-prone fetches behind an explicit flag
_web_search_fallback: bool = True   # degrade to the keyless ddgs provider when a selected one fails
_search_provider: str = "ddgs"
_brave_key: str = ""
_tavily_key: str = ""
_MAX_REDIRECTS = 5  # cap manual redirect following so each hop can be re-validated


def configure(config: dict) -> None:
    global _cfg, _searxng_url, _allow_private_fetch, _web_search_fallback
    global _search_provider, _brave_key, _tavily_key
    _cfg = config
    import sys
    from pathlib import Path
    scripts_dir = str(Path(__file__).parent.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import osenv
    from bob_core import _port  # single source of truth for ports
    agent = config.get("agent", {}) or {}
    _searxng_url = f"http://localhost:{_port(config, 'searxngPort')}/search"
    _allow_private_fetch = bool(agent.get("allowPrivateFetch", False))
    _web_search_fallback = bool(agent.get("webSearchFallback", True))
    _search_provider = (agent.get("searchProvider") or "ddgs").lower()
    _brave_key = osenv.secret("braveApiKey", agent.get("braveApiKey", ""), config)
    _tavily_key = osenv.secret("tavilyApiKey", agent.get("tavilyApiKey", ""), config)


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


def _fmt(rows: list) -> str | None:
    """Join (title, url, snippet) rows into the standard result block, or None if empty."""
    out = [f"- {t}\n  {u}\n  {(s or '')[:200]}" for t, u, s in rows if u]
    return "\n\n".join(out) if out else None


def _ddgs_search(query: str, num_results: int) -> str | None:
    """The default provider: the maintained pure-Python `ddgs` metasearch library (DuckDuckGo/Bing/
    Google), in-process, keyless, no Docker, identical on every OS. Returns None if the lib is missing
    or yields nothing, so the caller falls through to the stdlib scrape."""
    try:
        from ddgs import DDGS            # maintained package (formerly duckduckgo_search)
    except ImportError:
        try:
            from duckduckgo_search import DDGS   # older name, same API
        except ImportError:
            return None
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=num_results))
    except Exception:
        return None
    return _fmt([(x.get("title", ""), x.get("href") or x.get("url", ""),
                  x.get("body") or x.get("content", "")) for x in results[:num_results]])


def _brave_search(query: str, num_results: int) -> str | None:
    """Optional keyed provider: Brave's independent web index (agent.braveApiKey / BOB secret)."""
    if not _brave_key:
        return None
    r = requests.get("https://api.search.brave.com/res/v1/web/search",
                     params={"q": query, "count": num_results},
                     headers={"X-Subscription-Token": _brave_key, "Accept": "application/json"},
                     timeout=10)
    r.raise_for_status()
    results = ((r.json().get("web", {}) or {}).get("results", []))[:num_results]
    return _fmt([(x.get("title", ""), x.get("url", ""), x.get("description", "")) for x in results])


def _tavily_search(query: str, num_results: int) -> str | None:
    """Optional keyed provider: Tavily's LLM-optimized search (agent.tavilyApiKey / BOB secret)."""
    if not _tavily_key:
        return None
    r = requests.post("https://api.tavily.com/search",
                      json={"api_key": _tavily_key, "query": query, "max_results": num_results},
                      timeout=15)
    r.raise_for_status()
    results = r.json().get("results", [])[:num_results]
    return _fmt([(x.get("title", ""), x.get("url", ""), x.get("content", "")) for x in results])


def _ensure_searxng() -> str:
    """On-demand: bring the opt-in SearXNG Docker service up if it's down. Returns '' on success/already
    up, else a short reason. Only used when searxng is the selected provider."""
    try:
        import osenv
        from bob_core import _port
        if osenv.is_port_in_use(_port(_cfg, "searxngPort")):
            return ""                   # already up — no docker call
        import stack                    # scripts/tools is on sys.path (tool loader)
        ok, msg = stack.ensure_searxng(_cfg)
        return "" if ok else msg
    except Exception as e:              # noqa: BLE001 — advisory; fall through
        return str(e)


def _searxng_search(query: str, num_results: int) -> str | None:
    """Optional self-hosted meta-search via the SearXNG Docker service (started on demand if selected)."""
    if not _searxng_url:
        return None
    _ensure_searxng()
    r = requests.get(_searxng_url, params={"q": query, "format": "json", "pageno": 1}, timeout=10)
    r.raise_for_status()
    results = r.json().get("results", [])[:num_results]
    return _fmt([(x.get("title", ""), x.get("url", ""), x.get("content", "")) for x in results])


def _ddg_scrape_fallback(query: str, num_results: int) -> str | None:
    """Last-ditch keyless search: a thin HTML scrape of DuckDuckGo, used only if the `ddgs` library is
    unavailable. Best-effort parse of a public endpoint; returns None on any error / no results."""
    from html import unescape
    from urllib.parse import parse_qs, unquote
    try:
        r = requests.post("https://html.duckduckgo.com/html/", data={"q": query},
                          headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        r.raise_for_status()
    except Exception:
        return None
    titles = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.S)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', r.text, re.S)

    def _clean(html_text: str) -> str:
        return unescape(re.sub(r"<[^>]+>", "", html_text)).strip()

    rows = []
    for i, (href, title) in enumerate(titles[:num_results]):
        if "uddg=" in href:   # DDG wraps links as /l/?uddg=<encoded-target> — decode to the real URL.
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
        snippet = _clean(snippets[i]) if i < len(snippets) else ""
        rows.append((_clean(title), href, snippet))
    return _fmt(rows)


def _web_search(query: str, num_results: int = 5) -> str:
    """Run the selected provider, falling back to keyless ddgs then a stdlib scrape. Uniform on every OS,
    no Docker required by the default. The provider map is built here (not at import) so each entry
    resolves the current module-level fn — swappable in tests and never stale."""
    providers = {"ddgs": _ddgs_search, "brave": _brave_search,
                 "tavily": _tavily_search, "searxng": _searxng_search}
    order = [_search_provider]
    if _web_search_fallback and _search_provider != "ddgs":
        order.append("ddgs")
    errors = []
    for prov in order:
        fn = providers.get(prov)
        if fn is None:
            errors.append(f"{prov}: unknown provider (use ddgs|brave|tavily|searxng)")
            continue
        try:
            out = fn(query, num_results)
        except Exception as e:   # noqa: BLE001 — try the next provider
            errors.append(f"{prov}: {e}")
            continue
        if out:
            return out
        errors.append(f"{prov}: no results")
    fb = _ddg_scrape_fallback(query, num_results)   # last-ditch, only reached if ddgs is unavailable
    if fb:
        return fb
    return "web_search unavailable: " + "; ".join(errors)


def _web_fetch(url: str) -> str:
    # Allowlist http(s) and re-validate the host on EVERY hop: the initial URL AND each
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
            "description": "Search the web (default: in-process ddgs metasearch; optional Brave/Tavily/SearXNG providers). Cross-OS, no Docker required.",
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
