# BiblioGraph API

![CI](https://github.com/sameer26412/bibliograph-api/actions/workflows/ci.yml/badge.svg)

A headless bibliographic catalogue and circulation REST API built with **FastAPI**, **SQLAlchemy 2.0**, **Pydantic v2**, and **Neon PostgreSQL**. Designed for serverless deployment on Vercel with HTTP content negotiation (**JSON**, **Dublin Core XML**, **Schema.org JSON-LD**), transactional borrowing, and defense-in-depth security controls.

> **Live Interactive API Docs (`/docs`):** *(Updated upon deployment)*

---

## Architecture & Engineering Highlights

* **Serverless Database Pooling (Vercel + Neon):** Disables client-side ORM pooling (`poolclass=NullPool`) in PostgreSQL mode so each serverless invocation connects directly through Neon's `-pooler` (PgBouncer) and releases immediately, preventing connection exhaustion. Enforces strict `connect_timeout` and per-transaction `SET LOCAL statement_timeout`.
* **Web-Standard Content Negotiation:** `GET /api/v1/resources/{id}` dynamically serializes relational records into standard **JSON**, **Dublin Core XML** (`oai_dc` / `dc` namespaces), or **Schema.org `Book` JSON-LD** via either the HTTP `Accept` header (`application/xml`, `application/ld+json`) or `?format=json|xml|jsonld`.
* **ACID Circulation Transactions:** `POST /api/v1/loans` performs an atomic conditional inventory decrement (`available_copies > 0`) alongside duplicate-loan prevention, eliminating race conditions and negative stock.
* **Security & Abuse Protection:**
  * **SSRF Prevention:** Validates outbound `source_url` links at both schema validation and DNS-resolution time, blocking `localhost`, loopback (`127.0.0.0/8`, `::1`), RFC1918 private networks, cloud metadata (`169.254.169.254`), and redirects—with a strict **3.5s** outbound timeout.
  * **Payload & Query Bounds:** Outer ASGI middleware rejects bodies larger than **64 KB** (`413 Payload Too Large`) before JSON parsing; search queries are strictly bounded to **500 characters**; catalogue pagination is hard-capped to **1–50 items** (default 15); all schemas enforce `extra="forbid"`.
  * **Rate Limiting & Caching:** Sliding-window per-IP rate limiting (stricter tier on `/api/v1/auth/*`, paired with Vercel WAF at the edge), plus `Cache-Control: public, max-age=30, s-maxage=120, stale-while-revalidate=60` and SHA-256 `ETag` (`304 Not Modified`) headers on public catalogue reads.
  * **Auth & RBAC:** `bcrypt` password hashing, signed JWT Bearer tokens, and strict `member` vs. `librarian` authorization (`403 Forbidden`).

---

## Core Endpoints

| Method | Endpoint | Access | Description |
| :--- | :--- | :--- | :--- |
| `GET` | `/health` | Public | Verifies live database connectivity (`200` / `503`) |
| `POST` | `/api/v1/auth/register` | Public | Register a new `member` account |
| `POST` | `/api/v1/auth/login` | Public | Authenticate and receive JWT Bearer token |
| `GET` | `/api/v1/auth/me` | Bearer | Retrieve current authenticated user profile |
| `GET` | `/api/v1/resources` | Public | Search & filter catalogue (`q`, `author`, `tag`, `available`, `limit` 1–50, `offset`) |
| `GET` | `/api/v1/resources/{id}` | Public | Fetch single resource in JSON, Dublin Core XML, or Schema.org JSON-LD (`ETag` supported) |
| `POST` | `/api/v1/resources` | Librarian | Add a bibliographic resource with normalized authors & tags |
| `PATCH` | `/api/v1/resources/{id}` | Librarian | Update resource metadata or copy count |
| `DELETE` | `/api/v1/resources/{id}` | Librarian | Remove resource (blocked with `409` if active loans exist) |
| `POST` | `/api/v1/resources/{id}/verify-link` | Librarian | SSRF-safe, 3.5s timeout HTTP reachability check on `source_url` |
| `POST` | `/api/v1/loans` | Bearer | Atomically borrow an available copy |
| `POST` | `/api/v1/loans/{id}/return` | Bearer | Return an active loan and restore inventory |
| `GET` | `/api/v1/loans/me` | Bearer | List authenticated user's active or returned loans |

---

## Quick Start (Local Development)

```bash
python -m venv .venv
# Windows: .\.venv\Scripts\Activate.ps1
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload