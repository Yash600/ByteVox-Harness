"""Rule-based code scan: universal good-practice checks that need no project knowledge and no
model. Produces CANDIDATES with exact lines. Agents must still read the code to confirm them.

Python gets AST-based rules (precise); JS/TS get regex rules; every text file gets secret checks.
"""
import ast
import re
from pathlib import Path

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build", "_harness_web"}
SKIP_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock"}
PY_EXT = {".py"}
JS_EXT = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
SEV_RANK = {"high": 0, "medium": 1, "low": 2}

# The credential word may sit inside a longer name: ADMIN_PASSWORD, SECRET_KEY, db_passwd, AWS_SECRET_ACCESS_KEY
SECRET_RE = re.compile(r"""(?i)([A-Za-z0-9_]*(?:api[_-]?key|secret|token|passwd|password|private[_-]?key|access[_-]?key)[A-Za-z0-9_]*)\s*[:=]\s*['"]([^'"\s]{8,})['"]""")
PREFIX_RE = re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,})\b")
PEM_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
PLACEHOLDER = re.compile(r"(?i)(example|your[_-]|placeholder|changeme|xxxx|<.*>|\$\{.*\}|process\.env|os\.environ|getenv|\*{4,})")
TODO_RE = re.compile(r"(?i)#\s*(todo|fixme|hack|xxx)\b|//\s*(todo|fixme|hack|xxx)\b")

JS_RULES = [
    (re.compile(r"\beval\s*\("), "high", "js-eval", "eval() runs arbitrary code"),
    (re.compile(r"\bnew\s+Function\s*\("), "high", "js-new-function", "new Function() runs arbitrary code"),
    (re.compile(r"""\.innerHTML\s*\+?=(?!\s*["']{2}\s*;?\s*$)"""), "medium", "js-innerhtml", "assigning innerHTML can enable XSS"),
    (re.compile(r"dangerouslySetInnerHTML"), "medium", "js-dangerous-html", "dangerouslySetInnerHTML can enable XSS"),
    (re.compile(r"\bdocument\.write\s*\("), "medium", "js-document-write", "document.write can enable XSS"),
    (re.compile(r"catch\s*\([^)]*\)\s*\{\s*\}"), "medium", "js-empty-catch", "empty catch block swallows errors"),
    (re.compile(r"rejectUnauthorized\s*:\s*false"), "high", "js-tls-off", "TLS certificate verification disabled"),
    (re.compile(r"\bchild_process\b.*\bexec(Sync)?\s*\([^)]*(\+|`)"), "high", "js-cmd-injection", "shell command built from strings"),
]


_SQL_WORD = re.compile(r"(?i)\b(select|insert|update|delete|drop)\b")


def _builds_sql(node):
    """True if `node` assembles a string (concat / f-string / .format) containing SQL keywords."""
    builds = isinstance(node, (ast.BinOp, ast.JoinedStr)) or (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format")
    return builds and any(isinstance(c, ast.Constant) and isinstance(c.value, str) and _SQL_WORD.search(c.value)
                          for c in ast.walk(node))


class _PyVisitor(ast.NodeVisitor):
    def __init__(self, add):
        self.add = add
        self.sql_vars = {}     # name -> line where a SQL string was assembled from pieces

    def visit_ExceptHandler(self, n):
        if n.type is None:
            self.add(n.lineno, "medium", "py-bare-except", "bare `except:` also catches KeyboardInterrupt/SystemExit and hides errors")
        elif isinstance(n.type, ast.Name) and n.type.id in ("Exception", "BaseException") \
                and len(n.body) == 1 and isinstance(n.body[0], ast.Pass):
            self.add(n.lineno, "medium", "py-swallowed-exception", "exception caught and silently ignored")
        self.generic_visit(n)

    def visit_FunctionDef(self, n):
        for d in list(n.args.defaults) + [x for x in n.args.kw_defaults if x]:
            if isinstance(d, (ast.List, ast.Dict, ast.Set)):
                self.add(d.lineno, "medium", "py-mutable-default", "mutable default argument is shared between calls")
        self.generic_visit(n)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Assign(self, n):
        v = n.value
        if len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and _builds_sql(v):
            self.sql_vars[n.targets[0].id] = n.lineno
        if isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "open":
            self.add(n.lineno, "low", "py-open-no-with", "file opened without `with`; it may never be closed")
        self.generic_visit(n)

    def visit_BinOp(self, n):
        if isinstance(n.op, ast.Div) and isinstance(n.right, ast.Call) and isinstance(n.right.func, ast.Name) \
                and n.right.func.id == "len":
            self.add(n.lineno, "medium", "py-div-by-len", "division by len(...) raises ZeroDivisionError on an empty input")
        self.generic_visit(n)

    def visit_Call(self, n):
        f = n.func
        name = f.id if isinstance(f, ast.Name) else None
        attr = f.attr if isinstance(f, ast.Attribute) else None
        owner = f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else None
        kws = {k.arg: k.value for k in n.keywords if k.arg}
        if name in ("eval", "exec"):
            self.add(n.lineno, "high", "py-eval-exec", f"{name}() runs arbitrary code")
        if owner == "os" and attr == "system":
            self.add(n.lineno, "high", "py-os-system", "os.system with a built string risks command injection")
        if owner == "subprocess" and isinstance(kws.get("shell"), ast.Constant) and kws["shell"].value is True:
            self.add(n.lineno, "high", "py-shell-true", "subprocess with shell=True risks command injection")
        if owner == "requests" and attr in ("get", "post", "put", "delete", "patch", "head") and "timeout" not in kws:
            self.add(n.lineno, "medium", "py-no-timeout", "requests call without timeout can hang forever")
        if "verify" in kws and isinstance(kws["verify"], ast.Constant) and kws["verify"].value is False:
            self.add(n.lineno, "high", "py-tls-off", "TLS certificate verification disabled (verify=False)")
        if owner == "hashlib" and attr in ("md5", "sha1"):
            self.add(n.lineno, "medium", "py-weak-hash", f"{attr.upper()} is broken; do not use it for passwords or tokens")
        if owner in ("pickle", "marshal") and attr in ("load", "loads"):
            self.add(n.lineno, "high", "py-unsafe-deserialize", f"{owner}.{attr} on untrusted data can execute code")
        if owner == "yaml" and attr == "load" and "Loader" not in kws:
            self.add(n.lineno, "high", "py-yaml-load", "yaml.load without a safe Loader can execute code")
        if attr == "execute" and n.args:
            a = n.args[0]
            if isinstance(a, ast.Name) and a.id in self.sql_vars:
                self.add(self.sql_vars[a.id], "high", "py-sql-injection",
                         "SQL assembled from pieces and passed to execute(); use query parameters")
            if isinstance(a, ast.BinOp) or isinstance(a, ast.JoinedStr) or \
                    (isinstance(a, ast.Call) and isinstance(a.func, ast.Attribute) and a.func.attr == "format"):
                self.add(n.lineno, "high", "py-sql-injection", "SQL built by string concatenation/formatting; use parameters")
        self.generic_visit(n)


def scan_file(path: Path, rel: str):
    """Yield (line, severity, rule, message) for one file."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if "\0" in text[:2048] or len(text) > 2_000_000:
        return []
    out = []
    add = lambda line, sev, rule, msg: out.append((line, sev, rule, msg))
    ext = path.suffix.lower()
    if ext in PY_EXT:
        try:
            _PyVisitor(add).visit(ast.parse(text))
        except SyntaxError as e:
            add(e.lineno or 1, "high", "py-syntax-error", f"file does not parse: {e.msg}")
    lines = text.splitlines()
    for i, ln in enumerate(lines, 1):
        if ext in JS_EXT:
            for rx, sev, rule, msg in JS_RULES:
                if rx.search(ln):
                    add(i, sev, rule, msg)
        m = SECRET_RE.search(ln)
        if m and not PLACEHOLDER.search(m.group(2)) and not PLACEHOLDER.search(ln):
            add(i, "high", "hardcoded-secret", f"possible hardcoded {m.group(1).lower()} in source")
        elif PREFIX_RE.search(ln) and not PLACEHOLDER.search(ln):
            add(i, "high", "hardcoded-secret", "string looks like a real API key/token")
        if PEM_RE.search(ln):
            add(i, "high", "private-key", "private key committed to the repository")
        if TODO_RE.search(ln):
            add(i, "low", "todo-marker", "unfinished-work marker")
    return out


def scan_tree(root: Path, start: Path, rel_fn):
    """Scan a file or folder. Returns a list of (relpath, line, sev, rule, msg, code)."""
    files = [start] if start.is_file() else sorted(
        f for f in start.rglob("*") if f.is_file() and not (SKIP_DIRS & set(f.relative_to(root).parts))
        and f.name not in SKIP_NAMES and not f.name.endswith((".min.js", ".map")))
    found = []
    for f in files:
        rel = rel_fn(f)
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line, sev, rule, msg in scan_file(f, rel):
            code = lines[line - 1].strip()[:140] if 0 < line <= len(lines) else ""
            found.append((rel, line, sev, rule, msg, code))
    found.sort(key=lambda x: (SEV_RANK[x[2]], x[0], x[1]))
    return found


def format_findings(found, limit=60):
    if not found:
        return "static scan: no candidates found by the rule checks"
    head = f"static scan: {len(found)} candidate(s) from rule checks (unverified; confirm by reading the code)"
    body = [f"{r}:{ln}: [{sev}] {rule}: {msg} | {code}" for r, ln, sev, rule, msg, code in found[:limit]]
    if len(found) > limit:
        body.append(f"...({len(found) - limit} more not shown)")
    return head + "\n" + "\n".join(body)
