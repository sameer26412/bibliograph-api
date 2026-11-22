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


def _login(client: TestClient, email: str, password: str) -> str:
    res = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert res.status_code == 200, res.text
    return res.json()["access_token"]


def _register_and_login(
    client: TestClient,
    email: str,
    full_name: str,
    password: str,
    *,
    member_type: str = "student",
    semester: str | None = "5th Sem BCA",
) -> str:
    reg = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "full_name": full_name,
            "password": password,
            "phone": "+91 98765 43210",
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

    # Regular member cannot read admin circulation ledger
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

    # 1. Alice submits a borrow request -> stays pending, stock untouched (still 1)
    req = client.post(
        "/api/v1/loans",
        json={"resource_id": resource_id, "days": 7},
        headers=alice_headers,
    )
    assert req.status_code == 201
    loan_id = req.json()["id"]
    assert req.json()["status"] == "pending"
    assert client.get(f"/api/v1/resources/{resource_id}").json()["available_copies"] == 1

    # 2. Alice cannot self-approve or self-return
    assert client.post(f"/api/v1/loans/{loan_id}/approve", headers=alice_headers).status_code == 403
    assert client.post(f"/api/v1/loans/{loan_id}/return", headers=alice_headers).status_code == 403

    # 3. Librarian inspects ledger with full borrower profile & issues book -> stock drops to 0
    ledger = client.get("/api/v1/loans?status=pending", headers=lib_headers)
    assert ledger.status_code == 200
    assert ledger.json()[0]["borrower_semester"] == "6th Sem CS"
    assert ledger.json()[0]["borrower_phone"] == "+91 98765 43210"

    approved = client.post(
        f"/api/v1/loans/{loan_id}/approve",
        json={"days": 7, "admin_note": "Issued at desk"},
        headers=lib_headers,
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "active"
    original_due = approved.json()["due_at"]
    assert client.get(f"/api/v1/resources/{resource_id}").json()["available_copies"] == 0

    # 4. Bob cannot request while 0 copies remain
    assert (
        client.post(
            "/api/v1/loans",
            json={"resource_id": resource_id},
            headers=bob_headers,
        ).status_code
        == 409
    )

    # 5. Alice raises extension ticket -> Librarian approves -> due_at extended by 5 days
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

    # 6. Simulate 3 days overdue -> Librarian marks returned -> penalty cooldown activates
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

    # 7. Alice is blocked from borrowing again during her cooldown penalty
    blocked = client.post(
        "/api/v1/loans",
        json={"resource_id": resource_id, "days": 7},
        headers=alice_headers,
    )
    assert blocked.status_code == 403
    assert "Borrowing paused" in blocked.json()["detail"]

    # 8. Bob (no penalty) CAN now request the returned book
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