# Result: PARTIAL RESULT

**Goal:** Review this repo for bugs and security issues. Produce a prioritized list with evidence.

> **This result is incomplete.** Reasons:
> - s1 (Review core app files): unchecked - stopped: time limit reached (900s)
> - assembler unavailable (run: time limit reached (900s)); findings sorted by severity instead

## Summary
28 finding(s) were verified. The most important: Hardcoded admin password in source [s1.f1]; Hardcoded secret key for token generation [s1.f2]; Command injection via shell=True with user input [s1.f3]; SSRF with TLS verification disabled [s1.f4].

## Findings (most important first)

### 1. Hardcoded admin password in source  `high`  [hardcoded-secret]  (s1.f1) **[UNCHECKED - checker did not approve]**
Admin password is a plaintext constant in auth.py, exposing credentials to anyone with repo access.

- Evidence (verified) `library-app/library/auth.py:5`: `ADMIN_PASSWORD = "Library#2024!"`

### 2. Hardcoded secret key for token generation  `high`  [hardcoded-secret]  (s1.f2) **[UNCHECKED - checker did not approve]**
SECRET_KEY is a static string in source; anyone reading the code can forge valid session tokens.

- Evidence (verified) `library-app/library/auth.py:6`: `SECRET_KEY = "7d9f1c0e5b3a48f2a6c1e9d4b8f03a52"`

### 3. Command injection via shell=True with user input  `high`  [command-injection]  (s1.f3) **[UNCHECKED - checker did not approve]**
The 'name' query parameter is concatenated into a shell command, allowing arbitrary command execution.

- Evidence (verified) `library-app/library/server.py:15`: `subprocess.run("tar czf exports/" + name + ".tgz books.json", shell=True)`

### 4. SSRF with TLS verification disabled  `high`  [ssrf]  (s1.f4) **[UNCHECKED - checker did not approve]**
User-supplied URL is fetched with verify=False, enabling server-side request forgery and MITM attacks.

- Evidence (verified) `library-app/library/server.py:19`: `response = requests.get(url, verify=False)`

### 5. Path traversal via export filename  `high`  [security]  (s1.f6) **[UNCHECKED - checker did not approve]**
User-controlled 'name' param is concatenated into a file path, allowing writes to arbitrary locations (e.g. ../../etc/cron.d/x).

- Evidence (verified) `library-app/library/server.py:15`: `subprocess.run("tar czf exports/" + name + ".tgz books.json", shell=True)`

### 6. Unauthenticated /export endpoint allows arbitrary file export  `high`  [auth-bypass]  (s1.f11) **[UNCHECKED - checker did not approve]**
Any unauthenticated client can trigger tar export of books.json with no auth check on the /export path.

- Evidence (verified) `library-app/library/server.py:13`: `if parsed.path == "/export":`

### 7. Unauthenticated /cover endpoint acts as open SSRF proxy  `high`  [ssrf]  (s1.f12) **[UNCHECKED - checker did not approve]**
Any unauthenticated client can make the server fetch arbitrary URLs and return the content, enabling internal network scanning.

- Evidence (verified) `library-app/library/server.py:17`: `elif parsed.path == "/cover":`

### 8. SQL injection in find_by_title  `high`  [security]  (s2.f1)
User-supplied title is interpolated directly into SQL, allowing injection.

- Evidence (verified) `library-app/library/search.py:13`: `cur.execute(f"SELECT id, title, author FROM books WHERE title LIKE '%{title}%'")`

### 9. paginate off-by-one: page 1 skips first PAGE_SIZE items  `high`  [logic-bug]  (s2.f8)
start = page*size means page 1 returns items[size:2*size], skipping the first page.

- Evidence (verified) `library-app/library/utils.py:15`: `    start = page * size`

### 10. eval() on user input allows arbitrary code execution  `high`  [security]  (s3.f1)
applyFilter passes the raw filter field into eval(), letting any visitor run arbitrary JavaScript in the page context.

- Evidence (verified) `library-app/web/app.js:20`: `return books.filter((b) => eval(expr));`

### 11. innerHTML concatenation enables stored XSS  `high`  [security]  (s3.f2)
Book titles and authors are concatenated into innerHTML without escaping, so a crafted title can inject script.

- Evidence (verified) `library-app/web/app.js:14`: `list.innerHTML += "<li>" + b.title + " by " + b.author + "</li>";`

### 12. MD5 used for password hashing  `medium`  [weak-hash]  (s1.f5) **[UNCHECKED - checker did not approve]**
MD5 is cryptographically broken and unsuitable for password or token hashing; use bcrypt or argon2.

- Evidence (verified) `library-app/library/auth.py:10`: `return hashlib.md5(password.encode()).hexdigest()`

### 13. Missing Content-Length in HTTP responses  `medium`  [bug]  (s1.f7) **[UNCHECKED - checker did not approve]**
_reply never sends Content-Length or Content-Type headers, so clients may hang or misinterpret the response body.

- Evidence (verified) `library-app/library/server.py:25`: `self.send_response(status)`

### 14. No input validation on CLI arguments  `medium`  [bug]  (s1.f8) **[UNCHECKED - checker did not approve]**
int(argv[2]) and list indexing in app.py will crash with unhandled exceptions on malformed or missing arguments.

- Evidence (verified) `library-app/library/app.py:30`: `book = load_books()[int(argv[2]) - 1]`

### 15. Predictable session token (no randomness)  `medium`  [security]  (s1.f9) **[UNCHECKED - checker did not approve]**
make_token derives the token solely from username and a static SECRET_KEY, so any user who knows the key can forge valid tokens for any user.

- Evidence (verified) `library-app/library/auth.py:14`: `return hash_password(username + SECRET_KEY)`

### 16. Server binds to 0.0.0.0 with no authentication  `medium`  [security]  (s1.f10) **[UNCHECKED - checker did not approve]**
The HTTP server listens on all interfaces and the /export and /cover endpoints require no auth, exposing them to any network peer.

- Evidence (verified) `library-app/library/server.py:31`: `HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()`

### 17. No rate limiting or lockout on login attempts  `medium`  [brute-force]  (s1.f13) **[UNCHECKED - checker did not approve]**
login() can be called unlimited times with no throttling, enabling brute-force attacks against the single admin account.

- Evidence (verified) `library-app/library/auth.py:17`: `def login(username, password):`

### 18. Mutable default argument in add_book  `medium`  [bug]  (s2.f2)
tags=[] is shared across calls; 'new' tag accumulates on every invocation.

- Evidence (verified) `library-app/library/storage.py:22`: `def add_book(title, author, tags=[]):`

### 19. Bare except hides all errors in load_books  `medium`  [error-handling]  (s2.f3)
except: catches KeyboardInterrupt/SystemExit and silently returns empty list on any error.

- Evidence (verified) `library-app/library/storage.py:12`: `    except:`

### 20. Division by zero in average_rating  `medium`  [bug]  (s2.f4)  (found independently 2x)
Calling average_rating([]) raises ZeroDivisionError because len(ratings) is 0.

- Evidence (verified) `library-app/library/utils.py:10`: `    return sum(ratings) / len(ratings)`
- Evidence (verified) `library-app/library/utils.py:10`: `return sum(ratings) / len(ratings)`

### 21. late_fee computes days in wrong order  `medium`  [bug]  (s2.f5)
(due - returned) is negative when returned is after due, so late returns get a negative fee instead of a positive one.

- Evidence (verified) `library-app/library/utils.py:21`: `    days = (due - returned).days`

### 22. File handle never closed in load_books  `medium`  [resource-leak]  (s2.f6)
open(path) without with; file descriptor leaks on every call and on exception.

- Evidence (verified) `library-app/library/storage.py:9`: `    f = open(path)`

### 23. add_book id collision after deletions  `medium`  [logic-bug]  (s2.f7)
id is len(books)+1, so re-adding after a deletion reuses an existing id.

- Evidence (verified) `library-app/library/storage.py:26`: `    book = {"id": len(books) + 1, "title": title, "author": author, "tags": tags, "ratings": []}`

### 24. search.py connection and cursor never closed  `medium`  [resource-leak]  (s2.f9)
sqlite3 connection opened in find_by_title is never closed, leaking DB handles.

- Evidence (verified) `library-app/library/search.py:11`: `    conn = get_connection()`

### 25. Empty catch swallows all load errors  `medium`  [reliability]  (s3.f3)
The catch block in loadBooks discards the error, so fetch or JSON failures are invisible to the user and to logs.

- Evidence (verified) `library-app/web/app.js:7`: `} catch (e) {}`

### 26. Test hides pagination off-by-one bug  `medium`  [test-gap]  (s3.f5)
paginate docstring says pages are numbered from 1, but the test calls paginate(..., 0, 10) and expects the first page, masking the off-by-one.

- Evidence (verified) `library-app/tests/test_utils.py:14`: `assert paginate(list(range(25)), 0, 10) == list(range(10))`

### 27. requirements.txt missing Flask dependency  `medium`  [dependency]  (s3.f6)
The server module imports Flask but requirements.txt only lists requests, so a fresh install will fail at runtime.

- Evidence (verified) `library-app/requirements.txt:1`: `requests>=2.31`

### 28. Test does not cover empty-list case for average_rating  `medium`  [test-gap]  (s3.f7)
test_average only tests a non-empty list, so the ZeroDivisionError on empty input is never caught by the test suite.

- Evidence (verified) `library-app/tests/test_utils.py:10`: `assert average_rating([4, 5]) == 4.5`

## Subtasks

| id | kind | title | status | attempts | note |
|---|---|---|---|---|---|
| s1 | analyze | Review core app files | unchecked | 3 | stopped: time limit reached (900s) |
| s2 | analyze | Review data and utility files | accepted | 3 | Found SQL injection, mutable default, bare except, division by zero, and a logic bug in late_fee (subtracts in wrong ord |
| s3 | analyze | Review web, tests, and config | accepted | 2 | Reviewed web/app.js, tests/test_utils.py, README.md, and requirements.txt. The main new issues are a missing dependency  |

## Run stats

- LLM calls: 35/40  tokens: 63200/150000  seconds: 902.2/900
- Retries: 0  LLM errors: 0  injections flagged: 0  tool calls denied: 0
