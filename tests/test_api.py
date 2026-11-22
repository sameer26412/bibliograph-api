from datetime import timedelta
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import Base, get_db
import app.main as main_module
from app.models import Loan, utcnow
import app.security as security_module


@pytest.fixture()
def test_env(monkeypatch):
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(test_engine, "connect")
    def _enable_fk(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    testing_session_local = sessionmaker(
        bind=test_engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )

    monkeypatch.setattr(main_module, "engine", test_engine)
    monkeypatch.setattr(main_module, "SessionLocal", testing_session_local)
    monkeypatch.setattr(settings, "seed_demo_data", True)
    monkeypatch.setattr(settings, "seed_librarian_email", "librarian@bibliograph.dev")
    monkeypatch.setattr(settings, "seed_librarian_password", "LibrarianPass123!")
    monkeypatch.setattr(settings, "rate_limit_auth_per_minute", 100)

    def override_get_db():
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    main_module.app.dependency_overrides[get_db] = override_get_db
    security_module.rate_limiter.reset()

    with TestClient(main_module.app) as test_client:
        yield test_client, testing_session_local

    main_module.app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=test_engine)
    test_engine.dispose()


@pytest.fixture()
def client(test_env):
    return test_env[0]


def _login(
    client: TestClient,
    email: str,
    password: str,
    *,
    remember_me: bool = False,
) -> str:
    res = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password, "remember_me": remember_me},
    )
    assert res.status_code == 200, res.text
    return res.json()["access_token"]


def _register_and_login(
    client: TestClient,
    email: str,
    full_name: str,
    password: str,
    *,
    phone: str = "+91 98765 43210",
    member_type: str = "student",
    semester: str | None = "5th Sem BCA",
) -> str:
    reg = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "full_name": full_name,
            "password": password,
            "phone": phone,
            "address": "12 College Road, Haveri",
            "member_type": member_type,
            "semester": semester,
        },
    )
    assert reg.status_code == 201, reg.text
    return _login(client, email, password)


def test_root_ui_admin_and_health(client: TestClient):
    root = client.get("/")
    assert root.status_code == 200
    assert root.json()["ui"] == "/ui"
    assert root.json()["admin"] == "/admin"

    assert client.get("/ui").status_code == 200
    assert client.get("/admin").status_code == 200

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["database"] == "connected"
    assert health.headers["cache-control"] == "no-store"


def test_student_requires_semester_and_local_allows_none(client: TestClient):
    bad_student = client.post(
        "/api/v1/auth/register",
        json={
            "email": "nosem@bibliograph.dev",
            "full_name": "No Sem Student",
            "password": "StrongPassword1!",
            "phone": "+91 98765 00001",
            "address": "Main Street, Haveri",
            "member_type": "student",
        },
    )
    assert bad_student.status_code == 422

    ok_local = client.post(
        "/api/v1/auth/register",
        json={
            "email": "local@bibliograph.dev",
            "full_name": "Local Reader",
            "password": "StrongPassword1!",
            "phone": "+91 98765 00002",
            "address": "Station Road, Haveri",
            "member_type": "local",
        },
    )
    assert ok_local.status_code == 201
    assert ok_local.json()["semester"] is None


def test_remember_me_and_member_profile_management(client: TestClient):
    reg = client.post(
        "/api/v1/auth/register",
        json={
            "email": "rohan@bibliograph.dev",
            "full_name": "Rohan K",
            "password": "InitialPassword1!",
            "phone": "+91 98765 10101",
            "address": "Old Address, Haveri",
            "member_type": "student",
            "semester": "5th Sem BCA",
        },
    )
    assert reg.status_code == 201

    # Remember Me issues a 7-day token (604,800 seconds)
    login = client.post(
        "/api/v1/auth/login",
        json={
            "email": "rohan@bibliograph.dev",
            "password": "InitialPassword1!",
            "remember_me": True,
        },
    )
    assert login.status_code == 200
    assert login.json()["expires_in"] == 60 * 60 * 24 * 7
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    # Member updates own profile details
    updated = client.patch(
        "/api/v1/auth/me",
        json={"phone": "+91 98765 20202", "semester": "6th Sem BCA"},
        headers=headers,
    )
    assert updated.status_code == 200
    assert updated.json()["phone"] == "+91 98765 20202"
    assert updated.json()["semester"] == "6th Sem BCA"

    # Member changes password cleanly
    changed = client.post(
        "/api/v1/auth/change-password",
        json={
            "current_password": "InitialPassword1!",
            "new_password": "UpdatedPassword2!",
        },
        headers=headers,
    )
    assert changed.status_code == 204

    assert (
        client.post(
            "/api/v1/auth/login",
            json={"email": "rohan@bibliograph.dev", "password": "InitialPassword1!"},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/v1/auth/login",
            json={"email": "rohan@bibliograph.dev", "password": "UpdatedPassword2!"},
        ).status_code
        == 200
    )


def test_librarian_verified_password_recovery_flow(client: TestClient):
    _register_and_login(
        client,
        "locked@bibliograph.dev",
        "Locked Student",
        "OldSecretPass1!",
        phone="+91 98765 77777",
    )
    librarian_token = _login(client, "librarian@bibliograph.dev", "LibrarianPass123!")
    lib_headers = {"Authorization": f"Bearer {librarian_token}"}

    # Mismatched phone rejected
    bad_req = client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": "locked@bibliograph.dev", "phone": "+91 00000 00000"},
    )
    assert bad_req.status_code == 400

    # Verified email + phone queues request for desk
    ok_req = client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": "locked@bibliograph.dev", "phone": "9876577777"},
    )
    assert ok_req.status_code == 202

    pending = client.get("/api/v1/auth/password-reset/requests?status=pending", headers=lib_headers)
    assert pending.status_code == 200
    assert len(pending.json()) == 1
    req_id = pending.json()[0]["id"]

    # Librarian issues 6-digit recovery code
    issued = client.post(f"/api/v1/auth/password-reset/{req_id}/issue", headers=lib_headers)
    assert issued.status_code == 200
    code = issued.json()["recovery_code"]
    assert len(code) == 6
    assert issued.json()["issued_by_license"] == "LIB-001"

    # Wrong code rejected
    wrong_confirm = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={
            "email": "locked@bibliograph.dev",
            "code": "000000" if code != "000000" else "999999",
            "new_password": "RecoveredPass2!",
        },
    )
    assert wrong_confirm.status_code == 400

    # Valid code resets password and cannot be reused
    confirm = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={
            "email": "locked@bibliograph.dev",
            "code": code,
            "new_password": "RecoveredPass2!",
        },
    )
    assert confirm.status_code == 200

    reuse = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={
            "email": "locked@bibliograph.dev",
            "code": code,
            "new_password": "AnotherPass3!",
        },
    )
    assert reuse.status_code == 400

    # Student signs back in with recovered password
    assert _login(client, "locked@bibliograph.dev", "RecoveredPass2!")


def test_multi_librarian_shift_profiles_and_issuer_audit_trail(client: TestClient):
    head_token = _login(client, "librarian@bibliograph.dev", "LibrarianPass123!")
    head_headers = {"Authorization": f"Bearer {head_token}"}

    # Head Librarian licenses an Afternoon shift librarian
    created_staff = client.post(
        "/api/v1/auth/staff",
        json={
            "email": "priya@bibliograph.dev",
            "full_name": "Priya Nair",
            "password": "ShiftStaffPass1!",
            "phone": "+91 98765 55555",
            "address": "Reading Hall Desk 2",
            "staff_license_id": "LIB-002",
            "shift": "Afternoon",
        },
        headers=head_headers,
    )
    assert created_staff.status_code == 201
    staff_id = created_staff.json()["id"]
    assert created_staff.json()["staff_license_id"] == "LIB-002"
    assert created_staff.json()["is_head_librarian"] is False

    priya_token = _login(client, "priya@bibliograph.dev", "ShiftStaffPass1!")
    priya_headers = {"Authorization": f"Bearer {priya_token}"}

    # Shift librarian cannot register other staff (Head Librarian only)
    forbidden_staff = client.post(
        "/api/v1/auth/staff",
        json={
            "email": "unauth@bibliograph.dev",
            "full_name": "Unauthorized",
            "password": "ShiftStaffPass1!",
            "phone": "+91 98765 44444",
            "address": "Desk 3",
            "staff_license_id": "LIB-003",
            "shift": "Evening",
        },
        headers=priya_headers,
    )
    assert forbidden_staff.status_code == 403

    # Student requests book #1 -> Shift librarian Priya issues it -> audit trail visible to both
    student_token = _register_and_login(
        client, "auditstudent@bibliograph.dev", "Audit Student", "StudentPass123!"
    )
    student_headers = {"Authorization": f"Bearer {student_token}"}

    req = client.post(
        "/api/v1/loans",
        json={"resource_id": 1, "days": 7},
        headers=student_headers,
    )
    assert req.status_code == 201
    loan_id = req.json()["id"]

    issued = client.post(f"/api/v1/loans/{loan_id}/approve", headers=priya_headers)
    assert issued.status_code == 200
    assert issued.json()["issued_by_name"] == "Priya Nair"
    assert issued.json()["issued_by_license"] == "LIB-002"
    assert issued.json()["issued_by_shift"] == "Afternoon"

    my_loans = client.get("/api/v1/loans/me", headers=student_headers)
    assert my_loans.status_code == 200
    assert my_loans.json()[0]["issued_by_name"] == "Priya Nair"
    assert my_loans.json()[0]["issued_by_license"] == "LIB-002"

    # Head Librarian processes return -> both issuer and returner recorded
    returned = client.post(f"/api/v1/loans/{loan_id}/return", headers=head_headers)
    assert returned.status_code == 200
    assert returned.json()["issued_by_license"] == "LIB-002"
    assert returned.json()["returned_by_license"] == "LIB-001"

    # Head Librarian deactivates Priya -> existing token & new login both blocked with 403
    deactivated = client.patch(
        f"/api/v1/auth/staff/{staff_id}",
        json={"is_active": False},
        headers=head_headers,
    )
    assert deactivated.status_code == 200
    assert deactivated.json()["is_active"] is False

    assert client.get("/api/v1/auth/me", headers=priya_headers).status_code == 403
    assert (
        client.post(
            "/api/v1/auth/login",
            json={"email": "priya@bibliograph.dev", "password": "ShiftStaffPass1!"},
        ).status_code
        == 403
    )


def test_rare_book_waitlist_pre_assignment_auto_hold_and_fulfill(client: TestClient):
    lib_token = _login(client, "librarian@bibliograph.dev", "LibrarianPass123!")
    lib_headers = {"Authorization": f"Bearer {lib_token}"}

    alice_headers = {
        "Authorization": f"Bearer {_register_and_login(client, 'wa@bibliograph.dev', 'Alice A', 'AlicePass123!')}"
    }
    bob_headers = {
        "Authorization": f"Bearer {_register_and_login(client, 'wb@bibliograph.dev', 'Bob B', 'BobPass123!')}"
    }
    charlie_headers = {
        "Authorization": f"Bearer {_register_and_login(client, 'wc@bibliograph.dev', 'Charlie C', 'CharliePass123!')}"
    }

    # Register a 1-copy rare manuscript
    rare = client.post(
        "/api/v1/resources",
        json={
            "isbn": "978-0262033848",
            "title": "Introduction to Algorithms (Rare Edition)",
            "summary": "Single-copy reference edition.",
            "publication_year": 2009,
            "publisher": "MIT Press",
            "total_copies": 1,
            "authors": ["Thomas H. Cormen"],
            "tags": ["algorithms"],
        },
        headers=lib_headers,
    ).json()
    rid = rare["id"]

    # Alice borrows the sole copy -> stock drops to 0
    loan_id = client.post(
        "/api/v1/loans", json={"resource_id": rid, "days": 7}, headers=alice_headers
    ).json()["id"]
    assert client.post(f"/api/v1/loans/{loan_id}/approve", headers=lib_headers).status_code == 200
    assert client.get(f"/api/v1/resources/{rid}").json()["available_copies"] == 0

    # Public availability forecast exposes next expected return date without leaking borrower PII
    forecast = client.get("/api/v1/loans/availability").json()
    entry_forecast = next(f for f in forecast if f["resource_id"] == rid)
    assert entry_forecast["next_available_at"] is not None

    # Bob joins waitlist & Librarian pre-assigns the incoming return to Bob
    joined = client.post("/api/v1/loans/waitlist", json={"resource_id": rid}, headers=bob_headers)
    assert joined.status_code == 201
    wait_id = joined.json()["id"]
    assert joined.json()["status"] == "waiting"

    assigned = client.post(
        f"/api/v1/loans/waitlist/{wait_id}/assign",
        json={},
        headers=lib_headers,
    )
    assert assigned.status_code == 200
    assert assigned.json()["status"] == "assigned"
    assert assigned.json()["assigned_by_license"] == "LIB-001"

    # Alice returns the rare book -> copy auto-holds exclusively for Bob (public stock stays 0)
    assert client.post(f"/api/v1/loans/{loan_id}/return", headers=lib_headers).status_code == 200
    assert client.get(f"/api/v1/resources/{rid}").json()["available_copies"] == 0

    bob_wait = client.get("/api/v1/loans/waitlist/me", headers=bob_headers).json()
    assert bob_wait[0]["status"] == "ready"
    assert bob_wait[0]["hold_expires_at"] is not None

    # Charlie cannot jump the queue while Bob's hold is active
    assert (
        client.post(
            "/api/v1/loans", json={"resource_id": rid, "days": 7}, headers=charlie_headers
        ).status_code
        == 409
    )

    # Librarian issues the held copy directly to Bob -> active loan created & waitlist fulfilled
    fulfilled = client.post(
        f"/api/v1/loans/waitlist/{wait_id}/fulfill",
        json={"days": 10},
        headers=lib_headers,
    )
    assert fulfilled.status_code == 200
    assert fulfilled.json()["status"] == "active"
    assert fulfilled.json()["borrower_name"] == "Bob B"
    assert fulfilled.json()["issued_by_license"] == "LIB-001"

    bob_wait_after = client.get("/api/v1/loans/waitlist/me", headers=bob_headers).json()
    assert bob_wait_after[0]["status"] == "fulfilled"


def test_catalogue_pagination_and_search_limits(client: TestClient):
    default_page = client.get("/api/v1/resources")
    assert default_page.status_code == 200
    body = default_page.json()
    assert body["total"] == 6
    assert body["limit"] == 15
    assert body["has_more"] is False

    paged = client.get("/api/v1/resources?limit=2&offset=0")
    assert paged.status_code == 200
    assert len(paged.json()["items"]) == 2
    assert paged.json()["has_more"] is True

    assert client.get("/api/v1/resources?limit=51").status_code == 422
    assert client.get(f"/api/v1/resources?q={'x' * 501}").status_code == 422

    search = client.get("/api/v1/resources?q=Kleppmann")
    assert search.status_code == 200
    assert search.json()["total"] == 1


def test_content_negotiation_xml_jsonld_and_etag_caching(client: TestClient):
    xml_res = client.get("/api/v1/resources/1?format=xml")
    assert xml_res.status_code == 200
    assert "application/xml" in xml_res.headers["content-type"]
    assert "s-maxage=120" in xml_res.headers["cache-control"]
    root = ET.fromstring(xml_res.content)
    titles = [el.text for el in root.findall("{http://purl.org/dc/elements/1.1/}title")]
    assert titles == ["Designing Data-Intensive Applications"]

    xml_accept = client.get("/api/v1/resources/1", headers={"Accept": "application/xml"})
    assert xml_accept.status_code == 200
    assert "application/xml" in xml_accept.headers["content-type"]

    jld_res = client.get("/api/v1/resources/1", headers={"Accept": "application/ld+json"})
    assert jld_res.status_code == 200
    assert "application/ld+json" in jld_res.headers["content-type"]
    assert jld_res.json()["@type"] == "Book"

    etag = jld_res.headers["etag"]
    cached = client.get(
        "/api/v1/resources/1",
        headers={"Accept": "application/ld+json", "If-None-Match": etag},
    )
    assert cached.status_code == 304


def test_rbac_mass_assignment_and_resource_crud(client: TestClient):
    bad_reg = client.post(
        "/api/v1/auth/register",
        json={
            "email": "attacker@bibliograph.dev",
            "full_name": "Attacker",
            "password": "StrongPassword1!",
            "phone": "+91 98765 00000",
            "address": "Unknown Street",
            "member_type": "local",
            "role": "librarian",
        },
    )
    assert bad_reg.status_code == 422

    member_token = _register_and_login(
        client, "student@bibliograph.dev", "Student User", "StudentPass123!"
    )
    librarian_token = _login(client, "librarian@bibliograph.dev", "LibrarianPass123!")

    new_book = {
        "isbn": "978-0135957059",
        "title": "The Pragmatic Programmer",
        "summary": "Practical software engineering habits and system design.",
        "publication_year": 2019,
        "publisher": "Addison-Wesley",
        "language": "en",
        "source_url": "https://pragprog.com",
        "total_copies": 2,
        "authors": ["David Thomas", "Andrew Hunt"],
        "tags": ["software-engineering", "architecture"],
    }

    assert (
        client.post(
            "/api/v1/resources",
            json=new_book,
            headers={"Authorization": f"Bearer {member_token}"},
        ).status_code
        == 403
    )

    assert (
        client.get(
            "/api/v1/loans",
            headers={"Authorization": f"Bearer {member_token}"},
        ).status_code
        == 403
    )

    created = client.post(
        "/api/v1/resources",
        json=new_book,
        headers={"Authorization": f"Bearer {librarian_token}"},
    )
    assert created.status_code == 201
    resource_id = created.json()["id"]

    assert (
        client.post(
            "/api/v1/resources",
            json=new_book,
            headers={"Authorization": f"Bearer {librarian_token}"},
        ).status_code
        == 409
    )

    updated = client.patch(
        f"/api/v1/resources/{resource_id}",
        json={"total_copies": 4},
        headers={"Authorization": f"Bearer {librarian_token}"},
    )
    assert updated.status_code == 200
    assert updated.json()["available_copies"] == 4

    assert (
        client.delete(
            f"/api/v1/resources/{resource_id}",
            headers={"Authorization": f"Bearer {librarian_token}"},
        ).status_code
        == 204
    )


def test_request_approval_extension_and_overdue_penalty_workflow(test_env):
    client, session_factory = test_env
    librarian_token = _login(client, "librarian@bibliograph.dev", "LibrarianPass123!")
    alice_token = _register_and_login(
        client, "alice@bibliograph.dev", "Alice Student", "AlicePass123!", semester="6th Sem CS"
    )
    bob_token = _register_and_login(
        client, "bob@bibliograph.dev", "Bob Local", "BobPass123!", member_type="local", semester=None
    )

    lib_headers = {"Authorization": f"Bearer {librarian_token}"}
    alice_headers = {"Authorization": f"Bearer {alice_token}"}
    bob_headers = {"Authorization": f"Bearer {bob_token}"}

    book = client.post(
        "/api/v1/resources",
        json={
            "isbn": "978-0201633610",
            "title": "Design Patterns",
            "summary": "Elements of Reusable Object-Oriented Software.",
            "publication_year": 1994,
            "publisher": "Addison-Wesley",
            "total_copies": 1,
            "authors": ["Erich Gamma"],
            "tags": ["architecture"],
        },
        headers=lib_headers,
    ).json()
    resource_id = book["id"]

    req = client.post(
        "/api/v1/loans",
        json={"resource_id": resource_id, "days": 7},
        headers=alice_headers,
    )
    assert req.status_code == 201
    loan_id = req.json()["id"]
    assert req.json()["status"] == "pending"
    assert client.get(f"/api/v1/resources/{resource_id}").json()["available_copies"] == 1

    assert client.post(f"/api/v1/loans/{loan_id}/approve", headers=alice_headers).status_code == 403
    assert client.post(f"/api/v1/loans/{loan_id}/return", headers=alice_headers).status_code == 403

    ledger = client.get("/api/v1/loans?status=pending", headers=lib_headers)
    assert ledger.status_code == 200
    assert ledger.json()[0]["borrower_semester"] == "6th Sem CS"

    approved = client.post(
        f"/api/v1/loans/{loan_id}/approve",
        json={"days": 7, "admin_note": "Issued at desk"},
        headers=lib_headers,
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "active"
    original_due = approved.json()["due_at"]
    assert client.get(f"/api/v1/resources/{resource_id}").json()["available_copies"] == 0

    assert (
        client.post(
            "/api/v1/loans",
            json={"resource_id": resource_id},
            headers=bob_headers,
        ).status_code
        == 409
    )

    ext = client.post(
        f"/api/v1/loans/{loan_id}/extensions",
        json={"extra_days": 5, "reason": "Need extra days for exam prep"},
        headers=alice_headers,
    )
    assert ext.status_code == 201
    ext_id = ext.json()["id"]

    decided = client.post(
        f"/api/v1/loans/extensions/{ext_id}/decide",
        json={"decision": "approved", "admin_note": "Approved"},
        headers=lib_headers,
    )
    assert decided.status_code == 200
    assert decided.json()["status"] == "approved"
    assert decided.json()["current_due_at"] > original_due

    with session_factory() as db:
        loan_row = db.get(Loan, loan_id)
        loan_row.due_at = utcnow() - timedelta(days=3, hours=2)
        db.commit()

    returned = client.post(f"/api/v1/loans/{loan_id}/return", headers=lib_headers)
    assert returned.status_code == 200
    assert returned.json()["status"] == "returned"
    assert returned.json()["overdue_days"] >= 3
    assert returned.json()["borrower_suspended_until"] is not None
    assert client.get(f"/api/v1/resources/{resource_id}").json()["available_copies"] == 1

    blocked = client.post(
        "/api/v1/loans",
        json={"resource_id": resource_id, "days": 7},
        headers=alice_headers,
    )
    assert blocked.status_code == 403
    assert "Borrowing paused" in blocked.json()["detail"]

    bob_req = client.post(
        "/api/v1/loans",
        json={"resource_id": resource_id, "days": 7},
        headers=bob_headers,
    )
    assert bob_req.status_code == 201


@pytest.mark.parametrize(
    "blocked_url",
    [
        "http://127.0.0.1:8000/admin",
        "http://localhost/secret",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/internal",
        "http://192.168.1.1/router",
        "http://[::1]/loopback",
        "file:///etc/passwd",
    ],
)
def test_ssrf_blocks_internal_and_private_urls(client: TestClient, blocked_url: str):
    librarian_token = _login(client, "librarian@bibliograph.dev", "LibrarianPass123!")
    res = client.post(
        "/api/v1/resources",
        json={
            "isbn": "978-0000000001",
            "title": "SSRF Test",
            "publication_year": 2024,
            "publisher": "Security Lab",
            "source_url": blocked_url,
            "authors": ["Tester"],
        },
        headers={"Authorization": f"Bearer {librarian_token}"},
    )
    assert res.status_code == 422


def test_ssrf_blocks_dns_resolving_to_private_ip(monkeypatch):
    def fake_getaddrinfo(_host, _port, type=0):
        return [(2, 1, 6, "", ("192.168.1.55", 80))]

    monkeypatch.setattr(security_module.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError, match="private or non-public"):
        security_module.validate_public_http_url("https://internal.example.com", resolve_dns=True)


def test_oversized_payload_blocked_with_413(client: TestClient):
    oversized = '{"email":"a@b.com","full_name":"' + ("A" * 70_000) + '","password":"Password123!"}'
    res = client.post(
        "/api/v1/auth/register",
        content=oversized.encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    assert res.status_code == 413


def test_auth_rate_limiting_enforces_429(client: TestClient, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_auth_per_minute", 3)
    security_module.rate_limiter.reset()

    for _ in range(3):
        res = client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@bibliograph.dev", "password": "wrongpassword"},
        )
        assert res.status_code == 401

    limited = client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@bibliograph.dev", "password": "wrongpassword"},
    )
    assert limited.status_code == 429
    assert "retry-after" in limited.headers