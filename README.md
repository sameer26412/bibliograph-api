# BiblioGraph API

![CI](https://github.com/sameer26412/bibliograph-api/actions/workflows/ci.yml/badge.svg)

A librarian-governed bibliographic catalogue and circulation system built with **FastAPI**, **SQLAlchemy 2.0**, **Pydantic v2**, and **Neon PostgreSQL**. Combines dual lightweight web portals (**Member Portal** at `/ui` and **Standalone Librarian Console** at `/admin`) with an interoperable REST API supporting **JSON**, **Dublin Core XML**, and **Schema.org JSON-LD** content negotiation.

> **Live Deployment:**
> * Member Portal (`/ui`): *(Updated upon deployment)*
> * Librarian Console (`/admin`): *(Updated upon deployment)*
> * OpenAPI Swagger Docs (`/docs`): *(Updated upon deployment)*

---

## Key Capabilities (v2.0)

### 1. Dual Role-Separated Web Portals
* **Member Portal (`/ui`):** Clean light-mode library workspace with live debounced search, click-to-open Book Brief pop-ups (including an optional XML/JSON-LD metadata drawer), private **My Activity** tracking, and a dedicated **My Profile** view. Contains zero admin controls or cross-user visibility.
* **Standalone Librarian Console (`/admin`):** Restricted governance interface that rejects non-librarian logins and uses isolated browser session storage so staff and member workflows stay strictly separated.

### 2. Circulation Governance & Fair-Rotation Penalties
* **Request $\rightarrow$ Desk Approval:** Members submit borrow requests (`pending`) with a preferred duration (`1–30` days). Inventory is only decremented atomically when a licensed librarian clicks **Issue Book**.
* **Extension Tickets:** Active borrowers can raise extension tickets (`1–14` extra days + reason) for librarian approval or rejection.
* **Overdue Cooldown Enforcement:** Only librarians can mark physical returns. Late returns automatically calculate exact overdue days and set `suspended_until`, pausing new borrow requests and waitlist joins until the penalty period finishes.

### 3. Rare-Book Waitlist, Pre-Assignment & Calendar Reminders
* When a title reaches `0` available copies, members see the earliest expected return date (without exposing who holds the copy) and can join the waitlist.
* Librarians pre-assign incoming copies to waitlisted readers; upon return, the backend **automatically holds the copy exclusively** for that reader (keeping public stock at `0`) and triggers an in-app pickup alert.
* Members can download **1-click `.ics` calendar reminders** for due dates, expected returns, and pickup windows—compatible with Google Calendar, Apple Calendar, and Outlook with no third-party API keys.

### 4. Multi-Librarian Shift Profiles & Audit Trail
* Supports multiple shift librarians (`Morning`, `Afternoon`, `Evening`, `Full Day`) sharing a single database while signing in under individual licensed profiles (e.g., `LIB-001`, `LIB-002`).
* Head Librarian controls license creation, shift reassignment, and immediate account deactivation.
* Every loan, extension decision, waitlist assignment, and password reset records the handling librarian's name, license ID, and shift—visible in both the admin history and the borrower's activity view.

### 5. Persistent Sessions & Librarian-Verified Password Recovery
* **Remember Me:** Optional 7-day persistent JWT sessions alongside standard short-lived sessions.
* **Zero-SMTP Account Recovery:** Locked-out members verify their registered email and phone number to queue a reset request at `/admin`. Any on-duty librarian can generate a **15-minute single-use 6-digit recovery code** (stored `bcrypt`-hashed) so accounts and borrowed records are never permanently stuck.

### 6. Interoperable Metadata & Production Security
* **HTTP Content Negotiation:** `GET /api/v1/resources/{id}` dynamically serializes relational records into **JSON**, **Dublin Core XML** (`oai_dc`/`dc`), or **Schema.org `Book` JSON-LD** via `Accept` headers or `?format=json|xml|jsonld`, backed by SHA-256 `ETag` (`304 Not Modified`) and CDN `Cache-Control` headers.
* **Defense-in-Depth Security:**
  * **SSRF Protection:** Blocks `localhost`, loopback, RFC1918 private networks, cloud metadata (`169.254.169.254`), and redirects at both schema validation and DNS-resolution time with a **3.5s** outbound timeout.
  * **Abuse & Payload Guards:** Outer ASGI middleware rejects payloads over **64 KB** (`413`); search queries cap at **500 characters**; pagination hard-caps at **1–50 items**; all write schemas enforce `extra="forbid"`; sliding-window IP rate limiting protects `/api/v1/*` with stricter limits on auth routes.
  * **Serverless PostgreSQL Pooling:** Disables client-side ORM pooling (`NullPool`) in PostgreSQL mode to work cleanly with Neon's PgBouncer `-pooler`, enforcing `connect_timeout` and per-transaction `SET LOCAL statement_timeout`.

---

## Core API Endpoints

| Group | Method | Endpoint | Access | Description |
| :--- | :--- | :--- | :--- | :--- |
| **System** | `GET` | `/health` | Public | Verifies live database connectivity (`200` / `503`) |
| **Portals** | `GET` | `/ui`, `/admin` | Public / Staff Gate | Member Portal and Standalone Librarian Console |
| **Auth & Profiles** | `POST` | `/api/v1/auth/register`, `/login` | Public | Register member profile (`student`/`local`) or sign in (`remember_me`) |
| **Auth & Profiles** | `GET`, `PATCH` | `/api/v1/auth/me` | Bearer | Read or update own member profile |
| **Auth & Profiles** | `POST` | `/api/v1/auth/change-password` | Bearer | Update own password |
| **Recovery Desk** | `POST` | `/api/v1/auth/password-reset/request`, `/confirm` | Public | Request desk verification and confirm with 6-digit code |
| **Recovery Desk** | `GET`, `POST` | `/api/v1/auth/password-reset/requests`, `/{id}/issue` | Librarian | Review requests and generate 15-min single-use code |
| **Staff** | `GET`, `POST`, `PATCH` | `/api/v1/auth/staff`, `/staff/{id}` | Librarian / Head | View shift roster; Head Librarian licenses or deactivates staff |
| **Catalogue** | `GET` | `/api/v1/resources`, `/{id}` | Public | Live search/filter and JSON / Dublin Core XML / JSON-LD (`ETag`) |
| **Catalogue** | `POST`, `PATCH`, `DELETE` | `/api/v1/resources`, `/{id}`, `/{id}/verify-link` | Librarian | Manage catalogue and run SSRF-safe external link checks |
| **Circulation** | `POST`, `GET` | `/api/v1/loans`, `/loans/me`, `/loans/availability` | Bearer / Public | Request borrow, inspect own history, or check non-PII return forecasts |
| **Circulation** | `POST` | `/api/v1/loans/{id}/approve`, `/reject`, `/return` | Librarian | Issue copy atomically, decline request, or process return + overdue cooldown |
| **Extensions** | `POST`, `GET` | `/api/v1/loans/{id}/extensions`, `/extensions`, `/{id}/decide` | Bearer / Librarian | Submit due-date extension tickets and approve/decline |
| **Waitlist** | `POST`, `GET`, `DELETE` | `/api/v1/loans/waitlist`, `/waitlist/me`, `/{id}/assign`, `/{id}/fulfill` | Bearer / Librarian | Join waitlist, pre-assign incoming return, auto-hold, and issue |

---

## Quick Start (Local Development)

```bash
python -m venv .venv
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
# Linux / macOS:      source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload