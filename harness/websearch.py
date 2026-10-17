"""Web search with a backend chain. Keyed APIs first (reliable), then keyless scrapers
(best effort: scraped pages change and can block), then Wikipedia as a last resort.

Set TAVILY_API_KEY or BRAVE_API_KEY to use a proper search API; nothing else is needed.
"""
import base64
import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (compatible; bytevox-harness/0.1)"}


class SearchError(Exception):
    pass


def _http(url, data=None, headers=None, timeout=15):
    req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(2_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise SearchError(f"HTTP {e.code}")
    except (urllib.error.URLError, OSError) as e:
        raise SearchError(str(getattr(e, "reason", e)))


def _clean(s):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s or "")).split())


def _unwrap(href):
    """Search engines wrap result links in redirects; recover the real URL."""
    href = html.unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
    if "uddg" in q:                                   # DuckDuckGo
        return q["uddg"][0]
    host = urllib.parse.urlparse(href).netloc
    if host.endswith("bing.com") and "u" in q:        # Bing: u=a1<base64url(url)>
        u = q["u"][0]
        if u.startswith("a1"):
            b = u[2:] + "=" * (-len(u[2:]) % 4)
            try:
                return base64.urlsafe_b64decode(b).decode()
            except Exception:
                return ""
    return href


def _result(title, url, snippet):
    return {"title": _clean(title)[:160], "url": url, "snippet": _clean(snippet)[:320]}


def tavily(q, n):
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        raise SearchError("TAVILY_API_KEY not set")
    body = json.dumps({"api_key": key, "query": q, "max_results": n}).encode()
    d = json.loads(_http("https://api.tavily.com/search", body, {"Content-Type": "application/json"}))
    return [_result(r.get("title"), r.get("url"), r.get("content")) for r in d.get("results", [])]


def brave(q, n):
    key = os.environ.get("BRAVE_API_KEY")
    if not key:
        raise SearchError("BRAVE_API_KEY not set")
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": q, "count": n})
    d = json.loads(_http(url, headers={"X-Subscription-Token": key, "Accept": "application/json"}))
    return [_result(r.get("title"), r.get("url"), r.get("description"))
            for r in d.get("web", {}).get("results", [])]


def duckduckgo(q, n):
    page = _http("https://lite.duckduckgo.com/lite/", urllib.parse.urlencode({"q": q}).encode())
    links = re.findall(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*class=['\"]result-link['\"][^>]*>(.*?)</a>", page, re.S)
    snips = re.findall(r"class=['\"]result-snippet['\"][^>]*>(.*?)</td>", page, re.S)
    out = []
    for i, (href, title) in enumerate(links):
        out.append(_result(title, _unwrap(href), snips[i] if i < len(snips) else ""))
    if not out and re.search(r"anomaly|captcha|unusual traffic", page, re.I):
        raise SearchError("blocked by a bot check")
    return out


def bing(q, n):
    page = _http("https://www.bing.com/search?" + urllib.parse.urlencode({"q": q, "setlang": "en"}))
    out = []
    for block in re.split(r'<li class="b_algo"', page)[1:]:
        m = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        s = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        out.append(_result(m.group(2), _unwrap(m.group(1)), s.group(1) if s else ""))
    if not out and re.search(r"captcha|unusual traffic", page, re.I):
        raise SearchError("blocked by a bot check")
    return out


def wikipedia(q, n):
    url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
        {"action": "query", "list": "search", "srsearch": q, "format": "json", "srlimit": n})
    d = json.loads(_http(url))
    return [_result(r["title"], "https://en.wikipedia.org/wiki/" + urllib.parse.quote(r["title"].replace(" ", "_")),
                    r.get("snippet", "")) for r in d.get("query", {}).get("search", [])]


BACKENDS = [("tavily", tavily), ("brave", brave), ("duckduckgo", duckduckgo), ("bing", bing),
            ("wikipedia", wikipedia)]


def search(query, n=5):
    """Return (backend_name, results). Raises SearchError listing why every backend failed."""
    problems = []
    for name, fn in BACKENDS:
        try:
            res = fn(query, n)
        except (SearchError, json.JSONDecodeError, KeyError) as e:
            problems.append(f"{name}: {e}")
            continue
        seen, clean = set(), []
        for r in res:
            u = r.get("url") or ""
            if u.startswith("http") and u not in seen and "bing.com/aclk" not in u:
                seen.add(u)
                clean.append(r)
        if clean:
            return name, clean[:n]
        problems.append(f"{name}: no results")
    raise SearchError("all search backends failed (" + "; ".join(problems) + ")")
