"""Tool registry. Every tool works only inside the sandbox (or, for fetch_url, on public hosts).

Adding a tool = write one function + one entry in TOOLS. Which agent may call it is decided
in agents.py, never here.
"""
import ast
import ipaddress
import math
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

MAX_OUT = 8000
# Saved web pages live here. The name is deliberately odd: an ordinary `web/` folder is common in
# real projects (frontends) and must never be confused with, or hidden by, the harness's own files.
WEB_DIR = "_harness_web"
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}


class ToolError(Exception):
    pass


class Sandbox:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.sources = {}          # url -> saved path, so quotes from web pages can be verified
        self._n = 0
        self._lock = threading.Lock()

    def save_web(self, label, text, url=None):
        """Persist fetched/searched web text as a file under web/, so everything an agent
        learns is a real file that quotes can be checked against."""
        with self._lock:
            self._n += 1
            n = self._n
        slug = re.sub(r"[^A-Za-z0-9]+", "-", label)[:40].strip("-") or "page"
        rel = f"{WEB_DIR}/{n:02d}_{slug}.txt"
        p = self.resolve(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text((f"SOURCE: {url}\n\n" if url else "") + text, encoding="utf-8")
        if url:
            self.sources[url] = rel
        return rel

    def resolve(self, rel):
        if not isinstance(rel, str) or not rel.strip():
            raise ToolError("path must be a non-empty string")
        p = (self.root / rel).resolve()  # an absolute `rel` replaces root -> caught below
        if p != self.root and not p.is_relative_to(self.root):
            raise ToolError(f"path escapes the sandbox: {rel}")
        return p

    def rel(self, p):
        return p.relative_to(self.root).as_posix() or "."


def _int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _read_text(p):
    if p.stat().st_size > 5_000_000:
        raise ToolError("file larger than 1 MB")
    text = p.read_text(encoding="utf-8", errors="replace")
    if "\0" in text:
        raise ToolError("binary file")
    return text


def list_dir(sb, args):
    p = sb.resolve(args.get("path", "."))
    if not p.is_dir():
        raise ToolError(f"not a directory: {args.get('path')}")
    depth = max(1, min(_int(args.get("depth"), 2), 4))
    lines = []

    def walk(d, level):
        for c in sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
            if c.name in SKIP_DIRS or len(lines) >= 600:
                continue
            if c.is_dir():
                lines.append(sb.rel(c) + "/")
                if level < depth:
                    walk(c, level + 1)
            else:
                lines.append(f"{sb.rel(c)}  ({c.stat().st_size} bytes)")

    walk(p, 1)
    return "\n".join(lines) or "(empty directory)"


def read_file(sb, args):
    p = sb.resolve(_first(args, "path", "file", "file_path", "filename"))
    if not p.is_file():
        raise ToolError(f"file not found: {args.get('path')}")
    lines = _read_text(p).splitlines()
    start = max(1, _int(args.get("start_line"), 1))
    end = min(len(lines), _int(args.get("end_line"), start + 199))
    out = "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1))
    if end < len(lines):
        out += f"\n...(file has {len(lines)} lines; pass start_line/end_line to read more)"
    return out or "(empty file)"


def search_text(sb, args):
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ToolError("pattern must be a non-empty string")
    try:
        rx = re.compile(pattern, re.I)
    except re.error:
        rx = re.compile(re.escape(pattern), re.I)
    root = sb.resolve(args.get("path", "."))
    files = [root] if root.is_file() else sorted(f for f in root.rglob("*")
                                                 if f.is_file() and not (SKIP_DIRS & set(f.parts)))
    hits = []
    for f in files:
        try:
            text = _read_text(f)
        except (ToolError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append(f"{sb.rel(f)}:{i}: {line.strip()[:200]}")
                if len(hits) >= 100:
                    return "\n".join(hits) + "\n...(truncated at 100 matches)"
    return "\n".join(hits) or "no matches"


def edit_file(sb, args):
    p = sb.resolve(args.get("path"))
    old, new = args.get("old"), args.get("new")
    if not p.is_file():
        raise ToolError(f"file not found: {args.get('path')}")
    if not isinstance(old, str) or not old or not isinstance(new, str):
        raise ToolError("need string args: old (non-empty) and new")
    text = p.read_text(encoding="utf-8")
    n = text.count(old)
    if n != 1:
        raise ToolError(f"`old` must appear exactly once in the file (found {n})")
    p.write_text(text.replace(old, new), encoding="utf-8")
    return f"edited {sb.rel(p)}"


def edit_preview(sb, args):
    return (f"file: {args.get('path')}\n--- old\n{args.get('old')}\n+++ new\n{args.get('new')}")


# ---------------------------------------------------------------- fetch_url

_BLOCK_TAGS = {"p", "div", "li", "ul", "ol", "br", "tr", "td", "th", "table", "section", "article",
               "header", "footer", "nav", "aside", "main", "form", "hr", "blockquote", "pre", "dd", "dt",
               "dl", "figure", "figcaption", "h1", "h2", "h3", "h4", "h5", "h6", "caption"}
_SKIP_TAGS = {"script", "style", "noscript", "svg"}


class _Text(HTMLParser):
    """HTML -> readable text. Only BLOCK elements start a new line; inline elements (links,
    bold, citations) are joined into the sentence, so a sentence stays one line and quoting it
    naturally still matches the saved page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines, self.cur, self.skip = [], [], 0

    def _flush(self):
        s = " ".join("".join(self.cur).split())
        if s:
            self.lines.append(s)
        self.cur = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self.skip += 1
        elif tag in _BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
        elif tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        if not self.skip:
            self.cur.append(data)

    @property
    def parts(self):
        self._flush()
        return self.lines


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _check_public(url):
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ToolError("only http(s) URLs are allowed")
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80))
    except socket.gaierror as e:
        raise ToolError(f"cannot resolve host: {e}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ToolError("blocked: host resolves to a non-public address")


def fetch_url(sb, args):
    url = args.get("url")
    if not isinstance(url, str):
        raise ToolError("url must be a string")
    opener = urllib.request.build_opener(_NoRedirect)
    for _ in range(4):  # follow a few redirects manually, re-checking each hop
        _check_public(url)
        req = urllib.request.Request(url, headers={"User-Agent": "bytevox-harness/0.1"})
        try:
            with opener.open(req, timeout=30) as r:
                raw = r.read(1_000_000)
                ctype = r.headers.get("Content-Type", "")
            break
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                url = urllib.parse.urljoin(url, e.headers["Location"])
                continue
            raise ToolError(f"HTTP {e.code}")
        except (urllib.error.URLError, OSError) as e:
            raise ToolError(f"fetch failed: {e}")
    else:
        raise ToolError("too many redirects")
    ct = ctype.lower()
    if ct and not any(k in ct for k in ("text/", "json", "xml", "html")):
        raise ToolError(f"not a text page (content-type {ctype.split(';')[0]}); PDFs, images and "
                        "downloads are not supported")
    text = raw.decode("utf-8", errors="replace")
    if "html" in ct or text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        parser = _Text()
        parser.feed(text)
        text = "\n".join(parser.parts)
    if not text.strip():
        raise ToolError("the page has no readable text (it may need JavaScript)")
    rel = sb.save_web(urllib.parse.urlparse(url).netloc + "-" + urllib.parse.urlparse(url).path, text, url)
    n_lines = text.count("\n") + 1
    shown = text[:6000]
    more = (f"\n...(page continues: {n_lines} lines in total; read more with read_file on {rel} "
            "using start_line/end_line)") if len(text) > 6000 else ""
    return f"Saved as {rel} (source: {url}). Cite it as evidence file: {rel}\n\n{shown}{more}"


# ----------------------------------------------------------------- web_search

def web_search(sb, args):
    from . import websearch
    q = args.get("query")
    if not isinstance(q, str) or not q.strip():
        raise ToolError("query must be a non-empty string")
    n = max(1, min(_int(args.get("max_results"), 5), 8))
    try:
        backend, results = websearch.search(q.strip()[:200], n)
    except websearch.SearchError as e:
        raise ToolError(str(e))
    lines = [f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet']}" for i, r in enumerate(results, 1)]
    body = f"SEARCH: {q}  (via {backend})\n\n" + "\n".join(lines)
    rel = sb.save_web("search-" + q, body)
    return f"{body}\n\nSaved as {rel}. Use fetch_url on a result URL to read the full page."


# ------------------------------------------------------------------ read_files

def _first(args, *names):
    """First non-empty argument among common spellings of the same thing."""
    for n in names:
        if args.get(n):
            return args[n]
    return None


def read_files(sb, args):
    paths = _first(args, "paths", "files", "file_paths", "filenames", "path", "file")
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list) or not paths or not all(isinstance(x, str) for x in paths):
        raise ToolError('paths must be a non-empty list of file paths, for example {"paths": ["a.py", "b.py"]}')
    paths = paths[:8]
    budget = max(2000, min(_int(args.get("max_chars"), 8000), 12000))
    per = budget // len(paths)
    out = []
    for rel in paths:
        try:
            p = sb.resolve(rel)
            if not p.is_file():
                out.append(f"=== {rel} ===\nERROR: file not found")
                continue
            lines = _read_text(p).splitlines()
        except ToolError as e:
            out.append(f"=== {rel} ===\nERROR: {e}")
            continue
        body, used, shown = [], 0, 0
        for i, ln in enumerate(lines, 1):
            row = f"{i}: {ln}"
            if used + len(row) > per and shown:
                break
            body.append(row)
            used += len(row) + 1
            shown = i
        tail = f"\n...(showing lines 1-{shown} of {len(lines)}; use read_file with start_line to continue)" \
            if shown < len(lines) else ""
        out.append(f"=== {sb.rel(p)} ({len(lines)} lines) ===\n" + "\n".join(body) + tail)
    return "\n\n".join(out)


# ------------------------------------------------------------------- calculate

_CALC_FUNCS = {"abs": abs, "round": round, "min": min, "max": max, "sum": sum, "sqrt": math.sqrt,
               "log": math.log, "log10": math.log10, "ceil": math.ceil, "floor": math.floor}
_CALC_OPS = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
             ast.Div: lambda a, b: a / b, ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b,
             ast.Pow: lambda a, b: a ** b}


def _calc(node):
    if isinstance(node, ast.Expression):
        return _calc(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
            and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = _calc(node.operand)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.BinOp) and type(node.op) in _CALC_OPS:
        a, b = _calc(node.left), _calc(node.right)
        if isinstance(node.op, ast.Pow) and abs(b) > 100:
            raise ToolError("exponent too large")
        return _CALC_OPS[type(node.op)](a, b)
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_calc(e) for e in node.elts]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _CALC_FUNCS \
            and not node.keywords:
        args = [_calc(a) for a in node.args]
        if node.func.id in ("sum", "min", "max") and len(args) == 1:
            return _CALC_FUNCS[node.func.id](args[0])
        return _CALC_FUNCS[node.func.id](*args)
    raise ToolError("only arithmetic is allowed: numbers, + - * / // % **, parentheses, "
                    "and abs/round/min/max/sum/sqrt/log/log10/ceil/floor")


def calculate(sb, args):
    expr = args.get("expression")
    if not isinstance(expr, str) or not expr.strip() or len(expr) > 300:
        raise ToolError("expression must be a short string")
    try:
        return f"{expr.strip()} = {_calc(ast.parse(expr.strip(), mode='eval'))}"
    except ZeroDivisionError:
        raise ToolError("division by zero")
    except (SyntaxError, ValueError, OverflowError, TypeError) as e:
        raise ToolError(f"cannot evaluate: {e}")


def now(sb, args):
    t = time.gmtime()
    return time.strftime("Current UTC date and time: %Y-%m-%d %H:%M:%S (%A)", t)


# ----------------------------------------------------------------- static_scan

def static_scan(sb, args):
    from . import staticscan
    p = sb.resolve(args.get("path", "."))
    if not p.exists():
        raise ToolError(f"path not found: {args.get('path')}")
    found = staticscan.scan_tree(sb.root, p, sb.rel)
    return staticscan.format_findings(found)


# ------------------------------------------------------------- github_fetch

GH_HOSTS = {"codeload.github.com", "github.com"}
GH_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
GH_MAX_ZIP = 40 * 1024 * 1024        # compressed download
GH_MAX_UNZIPPED = 150 * 1024 * 1024  # declared uncompressed total (also the zip-bomb guard)
GH_MAX_FILES = 2500
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".tar", ".exe", ".dll",
            ".so", ".bin", ".woff", ".woff2", ".ttf", ".mp3", ".mp4", ".mov", ".whl", ".jar",
            ".pyc", ".class", ".o", ".a", ".lock"}


def _gh_download(url):
    opener = urllib.request.build_opener(_NoRedirect)
    for _ in range(4):
        host = urllib.parse.urlparse(url).hostname
        if host not in GH_HOSTS:
            raise ToolError(f"blocked: github_fetch only talks to {', '.join(sorted(GH_HOSTS))}")
        _check_public(url)
        req = urllib.request.Request(url, headers={"User-Agent": "bytevox-harness/0.1"})
        try:
            with opener.open(req, timeout=90) as r:
                buf = bytearray()
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        return bytes(buf)
                    buf += chunk
                    if len(buf) > GH_MAX_ZIP:
                        raise ToolError(f"repository is larger than {GH_MAX_ZIP // 1024 // 1024} MB; refusing")
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                url = urllib.parse.urljoin(url, e.headers["Location"])
                continue
            if e.code == 404:
                raise ToolError("repository or branch not found (is it public?)")
            if e.code in (403, 429):
                raise ToolError("GitHub is rate-limiting or refusing this download "
                                f"(HTTP {e.code}); the repo may be too large, or try again later")
            raise ToolError(f"GitHub returned HTTP {e.code}")
        except (urllib.error.URLError, OSError) as e:
            raise ToolError(f"download failed: {e}")
    raise ToolError("too many redirects")


def github_fetch(sb, args):
    """Download a PUBLIC GitHub repo (zip) into <sandbox>/repos/<owner>__<repo>/."""
    import io
    import zipfile
    owner, repo, ref = args.get("owner"), args.get("repo"), args.get("ref") or "HEAD"
    if not (isinstance(owner, str) and isinstance(repo, str) and GH_NAME.match(owner)
            and GH_NAME.match(repo)):
        raise ToolError("need `owner` and `repo` (letters, digits, - _ . only)")
    if not (isinstance(ref, str) and re.match(r"^[A-Za-z0-9_./-]{1,100}$", ref) and ".." not in ref):
        raise ToolError("invalid ref")
    data = _gh_download(f"https://codeload.github.com/{owner}/{repo}/zip/{ref}")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ToolError("GitHub did not return a valid zip")
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if sum(i.file_size for i in infos) > GH_MAX_UNZIPPED:
        raise ToolError(f"repository expands to more than {GH_MAX_UNZIPPED // 1024 // 1024} MB; refusing")
    dest = f"repos/{owner}__{repo}"
    kept, skipped, written = 0, {"binary/large": 0, "ignored dir": 0, "over file cap": 0}, 0
    for i in infos:
        parts = i.filename.split("/")[1:]          # drop the "<repo>-<ref>/" prefix
        if not parts or any(p in ("", "..") for p in parts) or i.filename.startswith("/"):
            continue                                # zip-slip guard
        if (i.external_attr >> 16) & 0o170000 == 0o120000:
            continue                                # symlink entry
        if SKIP_DIRS & set(parts):
            skipped["ignored dir"] += 1
            continue
        if Path(parts[-1]).suffix.lower() in SKIP_EXT or i.file_size > 3_000_000:
            skipped["binary/large"] += 1
            continue
        if kept >= GH_MAX_FILES:
            skipped["over file cap"] += 1
            continue
        content = zf.read(i)
        if b"\0" in content[:2048]:
            skipped["binary/large"] += 1
            continue
        target = sb.resolve(dest + "/" + "/".join(parts))   # re-checks it stays in the sandbox
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        kept += 1
        written += len(content)
    if not kept:
        raise ToolError("repository contained no readable text files")
    note = ", ".join(f"{n} {k}" for k, n in skipped.items() if n)
    return (f"Downloaded {owner}/{repo} ({ref}) into {dest}/ : {kept} text files, "
            f"{written // 1024} KB." + (f" Skipped: {note}." if note else ""))


@dataclass
class Tool:
    name: str
    doc: str
    fn: Callable
    risky: bool = False
    preview: Callable = None


TOOLS = {t.name: t for t in [
    Tool("list_dir", 'list_dir {"path": ".", "depth": 2} - list files in the sandbox', list_dir),
    Tool("read_file", 'read_file {"path": "a/b.py", "start_line": 1, "end_line": 200} - '
                      'read a file; lines are prefixed with their number', read_file),
    Tool("search_text", 'search_text {"pattern": "regex or text", "path": "."} - '
                        'find matching lines in sandbox files', search_text),
    Tool("read_files", 'read_files {"paths": ["a.py", "b.py"]} - read several small files in ONE call '
                       '(cheaper than many read_file calls); lines are numbered', read_files),
    Tool("static_scan", 'static_scan {"path": "."} - automatic rule-based scan of code for common problems '
                        '(secrets, injection, bare except, ...); returns unverified candidates with lines',
         static_scan),
    Tool("web_search", 'web_search {"query": "...", "max_results": 5} - search the web; returns titles, '
                       'URLs and snippets', web_search),
    Tool("fetch_url", 'fetch_url {"url": "https://..."} - download a PUBLIC web page as text; it is saved '
                      'as a file you can quote from', fetch_url),
    Tool("calculate", 'calculate {"expression": "(120*3)/7"} - exact arithmetic (use it instead of '
                      'doing maths yourself)', calculate),
    Tool("now", 'now {} - the current date and time (UTC)', now),
    Tool("github_fetch", 'github_fetch {"owner": "...", "repo": "..."} - download a PUBLIC GitHub repo '
                         'into the sandbox under repos/ (run by the harness before planning)', github_fetch),
    Tool("edit_file", 'edit_file {"path": "a.py", "old": "exact text", "new": "replacement"} - '
                      'replace text in a file (needs human approval)', edit_file,
         risky=True, preview=edit_preview),
]}


def tool_docs(names):
    return "\n".join("- " + TOOLS[n].doc for n in names) or "(no tools)"
