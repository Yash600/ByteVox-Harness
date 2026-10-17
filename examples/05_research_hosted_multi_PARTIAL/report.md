# Result: PARTIAL RESULT

**Goal:** What are the main differences between SQLite and PostgreSQL for a small web app? Give sources.

> **This result is incomplete.** Reasons:
> - s1 (SQLite facts for small web apps): unchecked - stopped: step limit reached (6 LLM calls)

## Summary
SQLite is a single-file, zero-config database that is easier to install and deploy for small web apps [s1.f1] [s1.f5]. It often performs well in server-side scenarios where the application serializes requests, mitigating its limited concurrent-write model [s1.f2] [s1.f3]. In contrast, PostgreSQL is a powerful, open-source object-relational database with nearly 40 years of development [s2.f1]. It offers superior concurrency through MVCC, ensuring non-blocking read/write operations [s2.f3], and provides high reliability with ACID compliance since 2001 [s2.f2]. Additionally, PostgreSQL supports a wider range of SQL:2023 features and rich data types like JSONB and custom types, offering greater extensibility for complex web applications [s2.f4] [s2.f5].

## Findings (most important first)

### 1. SQLite is a single-file, zero-config database  `high`  [features]  (s1.f1) **[UNCHECKED - checker did not approve]**
SQLite stores the entire database in a single cross-platform file, making it easy to install, deploy, and back up for small web apps.

- Evidence (verified) `web/04_www-sqlite-org-whentouse-html.txt` <https://www.sqlite.org/whentouse.html>: `the resulting database is a single file that can be written to a USB memory stick or emailed to a colleague.`

### 2. PostgreSQL is a powerful, open-source object-relational DB  `high`  [overview]  (s2.f1)
Official site describes PostgreSQL as a powerful, open source object-relational database with nearly 40 years of development.

- Evidence (verified) `web/06_www-postgresql-org-about.txt:33` <https://www.postgresql.org/about/>: `PostgreSQL is a powerful, open source object-relational database system that uses and extends the SQL language combined with many features that safely store and scale the most complicated data workloa`

### 3. MVCC concurrency model  `high`  [concurrency]  (s2.f3)
PostgreSQL uses Multi-Version Concurrency Control (MVCC) for concurrent read/write operations without blocking.

- Evidence (verified) `web/06_www-postgresql-org-about.txt:76` <https://www.postgresql.org/about/>: `Multi-Version concurrency Control (MVCC)`

### 4. SQLite works as a server-side data store  `high`  [suitability]  (s1.f2) **[UNCHECKED - checker did not approve]**
Developers report success using SQLite as the storage engine for application-specific web servers, often faster than a client/server SQL engine in that scenario.

- Evidence (verified) `web/04_www-sqlite-org-whentouse-html.txt` <https://www.sqlite.org/whentouse.html>: `Developers report that SQLite is often faster than a client/server SQL database engine in this scenario.`

### 5. ACID-compliant since 2001  `high`  [reliability]  (s2.f2)
PostgreSQL has been ACID-compliant since 2001, ensuring transactional integrity for web apps.

- Evidence (verified) `web/06_www-postgresql-org-about.txt` <https://www.postgresql.org/about/>: `PostgreSQL runs on all major operating systems , has been ACID -compliant since 2001, and has powerful add-ons such as the popular PostGIS geospatial database extender.`

### 6. Rich data types and extensibility  `medium`  [features]  (s2.f5)
Supports JSON/JSONB, arrays, UUIDs, custom types, and procedural languages (Python, Perl, etc.) for flexible web app storage.

- Evidence (verified) `web/06_www-postgresql-org-about.txt:62` <https://www.postgresql.org/about/>: `Document: JSON/JSONB, XML, Key-value (Hstore)`

### 7. Concurrency is managed by the server, not SQLite  `medium`  [concurrency]  (s1.f3) **[UNCHECKED - checker did not approve]**
In the server-side pattern, the application serializes database requests, so SQLite's limited concurrent-write model is not a bottleneck for small apps.

- Evidence (verified) `web/04_www-sqlite-org-whentouse-html.txt` <https://www.sqlite.org/whentouse.html>: `Database requests are serialized by the server, so concurrency is not an issue.`

### 8. SQL:2023 Core conformance  `medium`  [standards]  (s2.f4)
As of v18 (Sept 2025), PostgreSQL conforms to at least 170 of 177 mandatory SQL:2023 Core features.

- Evidence (verified) `web/06_www-postgresql-org-about.txt:55` <https://www.postgresql.org/about/>: `As of the version 18 release in September 2025, PostgreSQL conforms to at least 170 of the 177 mandatory features for SQL:2023 Core conformance.`

### 9. SQLite is easier to install and use than an enterprise RDBMS  `low`  [suitability]  (s1.f5) **[UNCHECKED - checker did not approve]**
For small apps, SQLite's main advantage over a client/server database is simpler installation and a self-contained file format.

- Evidence (verified) `web/04_www-sqlite-org-whentouse-html.txt` <https://www.sqlite.org/whentouse.html>: `The advantage of SQLite is that it is easier to install and use and the resulting database is a single file`

### 10. Database sharding scales SQLite for many connections  `medium`  [concurrency]  (s1.f4) **[UNCHECKED - checker did not approve]**
Using a separate SQLite file per user/subdomain lets a server handle hundreds or thousands of simultaneous connections, each hitting its own file.

- Evidence (verified) `web/04_www-sqlite-org-whentouse-html.txt` <https://www.sqlite.org/whentouse.html>: `the server might have a separate SQLite database for each user, so that the server can handle hundreds or thousands of simultaneous connections`

## Subtasks

| id | kind | title | status | attempts | note |
|---|---|---|---|---|---|
| s1 | research | SQLite facts for small web apps | unchecked | 1 | stopped: step limit reached (6 LLM calls) |
| s2 | research | PostgreSQL facts for small web apps | accepted | 1 | PostgreSQL is a powerful, open-source, ACID-compliant object-relational database with MVCC concurrency, rich data types, |

## Run stats

- LLM calls: 22/60  tokens: 40523/120000  seconds: 607.3/900
- Retries: 1  LLM errors: 1  injections flagged: 0  tool calls denied: 0
