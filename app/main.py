from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import Base, SessionLocal, engine, get_db
from app.models import Author, Resource, Tag, User
from app.routers import auth, loans, resources
from app.schemas import HealthOut
from app.security import (
    MaxBodySizeMiddleware,
    extract_client_ip,
    hash_password,
    rate_limiter,
)

UI_HTML_PATH = Path(__file__).resolve().parent / "ui.html"
ADMIN_HTML_PATH = Path(__file__).resolve().parent / "admin.html"

DEMO_RESOURCES = [
    {
        "isbn": "978-1449373320",
        "title": "Designing Data-Intensive Applications",
        "summary": "Core principles of reliable, scalable, and maintainable distributed data systems.",
        "publication_year": 2017,
        "publisher": "O'Reilly Media",
        "language": "en",
        "source_url": "https://dataintensive.net",
        "total_copies": 4,
        "authors": ["Martin Kleppmann"],
        "tags": ["distributed-systems", "databases", "architecture"],
    },
    {
        "isbn": "978-0470015544",
        "title": "Web Engineering: The Discipline of Systematic Development of Web Applications",
        "summary": "Systematic methods for modeling, engineering, testing, and evolving web applications.",
        "publication_year": 2006,
        "publisher": "Wiley",
        "language": "en",
        "source_url": "https://www.wiley.com",
        "total_copies": 3,
        "authors": ["Gerti Kappel", "Birgit Pröll", "Siegfried Reich", "Werner Retschitzegger"],
        "tags": ["web-engineering", "software-engineering", "architecture"],
    },
    {
        "isbn": "978-1491900246",
        "title": "RESTful Web APIs: Services for a Changing World",
        "summary": "Practical hypermedia, content negotiation, and HTTP protocol design for web APIs.",
        "publication_year": 2013,
        "publisher": "O'Reilly Media",
        "language": "en",
        "source_url": "https://www.oreilly.com",
        "total_copies": 3,
        "authors": ["Leonard Richardson", "Mike Amundsen", "Sam Ruby"],
        "tags": ["rest", "http", "web-engineering"],
    },
    {
        "isbn": "978-0521865715",
        "title": "Introduction to Information Retrieval",
        "summary": "Foundations of web search, indexing, XML retrieval, and metadata ranking.",
        "publication_year": 2008,
        "publisher": "Cambridge University Press",
        "language": "en",
        "source_url": "https://nlp.stanford.edu/IR-book/",
        "total_copies": 2,
        "authors": ["Christopher D. Manning", "Prabhakar Raghavan", "Hinrich Schütze"],
        "tags": ["information-retrieval", "xml", "search"],
    },
    {
        "isbn": "978-0262044684",
        "title": "A Semantic Web Primer",
        "summary": "RDF, OWL, Linked Data, and machine-readable metadata technologies for the web.",
        "publication_year": 2012,
        "publisher": "MIT Press",
        "language": "en",
        "source_url": "https://www.w3.org/TR/json-ld11/",
        "total_copies": 2,
        "authors": ["Grigoris Antoniou", "Paul Groth", "Frank van Harmelen", "Rinke Hoekstra"],
        "tags": ["semantic-web", "json-ld", "linked-data"],
    },
    {
        "isbn": "978-0134685991",
        "title": "Database System Concepts",
        "summary": "Relational schema design, ACID transactions, indexing, and query optimization.",
        "publication_year": 2019,
        "publisher": "McGraw-Hill",
        "language": "en",
        "source_url": "https://www.db-book.com",
        "total_copies": 5,
        "authors": ["Abraham Silberschatz", "Henry F. Korth", "S. Sudarshan"],
        "tags": ["databases", "sql", "transactions"],
    },
]


def init_db_and_seed() -> None:
    Base.metadata.create_all(bind=engine)
    if not settings.seed_demo_data:
        return

    db = SessionLocal()
    try:
        librarian_email = settings.seed_librarian_email.lower()
        existing_librarian = db.scalar(select(User).where(User.email == librarian_email))
        if existing_librarian is None:
            demo_member_email = "member@bibliograph.dev"
        existing_demo_member = db.scalar(select(User).where(User.email == demo_member_email))
        if existing_demo_member is None:
            db.add(
                User(
                    email=demo_member_email,
                    full_name="Demo Student",
                    phone="+91 90000 00001",
                    address="Campus Reading Block, Haveri",
                    member_type="student",
                    semester="6th Sem BCA",
                    is_active=True,
                    hashed_password=hash_password("MemberDemo2026!"),
                    role="member",
                )
            )
            db.commit()
            db.add(
                User(
                    email=librarian_email,
                    full_name="Head Librarian",
                    phone="+00-0000000000",
                    address="Central Library Desk",
                    member_type="local",
                    semester=None,
                    staff_license_id="LIB-001",
                    shift="Full Day",
                    is_head_librarian=True,
                    is_active=True,
                    hashed_password=hash_password(settings.seed_librarian_password),
                    role="librarian",
                )
            )
            db.commit()

        existing_count = db.scalar(select(func.count(Resource.id))) or 0
        if existing_count == 0:
            author_cache: dict[str, Author] = {}
            tag_cache: dict[str, Tag] = {}

            for item in DEMO_RESOURCES:
                item_authors: list[Author] = []
                for name in item["authors"]:
                    key = name.lower()
                    if key not in author_cache:
                        author_cache[key] = Author(name=name)
                        db.add(author_cache[key])
                    item_authors.append(author_cache[key])

                item_tags: list[Tag] = []
                for name in item["tags"]:
                    if name not in tag_cache:
                        tag_cache[name] = Tag(name=name)
                        db.add(tag_cache[name])
                    item_tags.append(tag_cache[name])

                db.add(
                    Resource(
                        isbn=item["isbn"],
                        title=item["title"],
                        summary=item["summary"],
                        publication_year=item["publication_year"],
                        publisher=item["publisher"],
                        language=item["language"],
                        source_url=item["source_url"],
                        total_copies=item["total_copies"],
                        available_copies=item["total_copies"],
                        authors=item_authors,
                        tags=item_tags,
                    )
                )
            db.commit()
    except IntegrityError:
        db.rollback()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db_and_seed()
    yield


app = FastAPI(
    title="BiblioGraph API",
    version="1.0.0",
    description=(
        "Headless bibliographic catalogue and librarian-governed circulation REST API with HTTP "
        "content negotiation (JSON, Dublin Core XML, Schema.org JSON-LD), approval workflows, "
        "extension tickets, overdue cooldown penalties, SSRF protection, and serverless pooling."
    ),
    lifespan=lifespan,
)


@app.middleware("http")
async def security_and_rate_limit_middleware(request: Request, call_next):
    path = request.url.path
    rate_headers: dict[str, str] = {}

    if path.startswith("/api/v1/"):
        ip = extract_client_ip(request)
        if path.startswith("/api/v1/auth"):
            limit = settings.rate_limit_auth_per_minute
            bucket_key = f"auth:{ip}"
        else:
            limit = settings.rate_limit_default_per_minute
            bucket_key = f"api:{ip}"

        allowed, remaining, retry_after = rate_limiter.check(bucket_key, limit)
        rate_headers = {
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": str(remaining),
        }
        if not allowed:
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "Rate limit exceeded. Please retry later."},
                headers={
                    **rate_headers,
                    "Retry-After": str(retry_after),
                    "Cache-Control": "no-store",
                },
            )

    response = await call_next(request)
    for key, value in rate_headers.items():
        response.headers[key] = value
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


app.add_middleware(MaxBodySizeMiddleware, max_bytes=settings.max_request_body_bytes)

app.include_router(auth.router)
app.include_router(resources.router)
app.include_router(loans.router)


@app.get("/", tags=["System"])
def root(request: Request):
    accept = request.headers.get("accept", "")
    if "text/html" in accept and "application/json" not in accept:
        return RedirectResponse(url="/ui", status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    return {
        "service": "BiblioGraph API",
        "version": "2.0.0",
        "ui": "/ui",
        "admin": "/admin",
        "docs": "/docs",
        "health": "/health",
        "catalogue": "/api/v1/resources",
    }


@app.get("/ui", response_class=HTMLResponse, include_in_schema=False)
def member_web_ui() -> HTMLResponse:
    return HTMLResponse(
        content=UI_HTML_PATH.read_text(encoding="utf-8"),
        headers={"Cache-Control": "public, max-age=60"},
    )


@app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
def admin_web_ui() -> HTMLResponse:
    return HTMLResponse(
        content=ADMIN_HTML_PATH.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/health", response_model=HealthOut, tags=["System"])
def health_check(
    response: Response,
    db: Session = Depends(get_db),
) -> HealthOut:
    response.headers["Cache-Control"] = "no-store"
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "degraded",
                "database": "unreachable",
                "environment": settings.app_env,
            },
            headers={"Cache-Control": "no-store"},
        )
    return HealthOut(
        status="ok",
        database="connected",
        environment=settings.app_env,
    )