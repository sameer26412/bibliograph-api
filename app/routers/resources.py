import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.deps import require_librarian
from app.models import Author, Loan, Resource, Tag, User
from app.schemas import (
    LinkVerificationOut,
    PaginatedResources,
    ResourceCreate,
    ResourceOut,
    ResourceUpdate,
)
from app.security import verify_external_url
from app.serializers import (
    build_etag,
    resolve_response_format,
    serialize_resource_dublin_core_xml,
    serialize_resource_jsonld,
)

router = APIRouter(prefix="/api/v1/resources", tags=["Catalogue & Metadata"])

PUBLIC_CACHE_CONTROL = "public, max-age=30, s-maxage=120, stale-while-revalidate=60"


def _get_or_create_authors(db: Session, names: list[str]) -> list[Author]:
    lowered = [name.lower() for name in names]
    existing = db.scalars(
        select(Author).where(func.lower(Author.name).in_(lowered))
    ).all()
    by_lower = {author.name.lower(): author for author in existing}

    result: list[Author] = []
    for name in names:
        key = name.lower()
        author = by_lower.get(key)
        if author is None:
            author = Author(name=name)
            db.add(author)
            by_lower[key] = author
        result.append(author)
    return result


def _get_or_create_tags(db: Session, names: list[str]) -> list[Tag]:
    if not names:
        return []
    existing = db.scalars(select(Tag).where(Tag.name.in_(names))).all()
    by_name = {tag.name: tag for tag in existing}

    result: list[Tag] = []
    for name in names:
        tag = by_name.get(name)
        if tag is None:
            tag = Tag(name=name)
            db.add(tag)
            by_name[name] = tag
        result.append(tag)
    return result


def _load_resource_or_404(db: Session, resource_id: int) -> Resource:
    resource = db.scalar(
        select(Resource)
        .options(selectinload(Resource.authors), selectinload(Resource.tags))
        .where(Resource.id == resource_id)
    )
    if resource is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Resource {resource_id} not found.",
        )
    return resource


def _etag_matches(request: Request, etag: str) -> bool:
    inm = request.headers.get("if-none-match")
    if not inm:
        return False
    candidates = [part.strip() for part in inm.split(",")]
    return etag in candidates or f"W/{etag}" in candidates


@router.get("", response_model=PaginatedResources)
def list_resources(
    request: Request,
    q: str | None = Query(
        default=None,
        max_length=500,
        description="Search title, summary, ISBN, publisher, author, or tag (max 500 chars).",
    ),
    author: str | None = Query(default=None, max_length=120),
    tag: str | None = Query(default=None, max_length=60),
    available: bool | None = Query(default=None),
    limit: int = Query(default=15, ge=1, le=50, description="Hard-capped between 1 and 50."),
    offset: int = Query(default=0, ge=0, le=10_000),
    db: Session = Depends(get_db),
) -> Response:
    filters = []

    if q and q.strip():
        pattern = f"%{q.strip()}%"
        filters.append(
            or_(
                Resource.title.ilike(pattern),
                Resource.summary.ilike(pattern),
                Resource.isbn.ilike(pattern),
                Resource.publisher.ilike(pattern),
                Resource.authors.any(Author.name.ilike(pattern)),
                Resource.tags.any(Tag.name.ilike(pattern)),
            )
        )

    if author and author.strip():
        filters.append(Resource.authors.any(Author.name.ilike(f"%{author.strip()}%")))

    if tag and tag.strip():
        filters.append(Resource.tags.any(Tag.name == tag.strip().lower()))

    if available is True:
        filters.append(Resource.available_copies > 0)
    elif available is False:
        filters.append(Resource.available_copies == 0)

    total = db.scalar(select(func.count(Resource.id)).where(*filters)) or 0
    rows = db.scalars(
        select(Resource)
        .options(selectinload(Resource.authors), selectinload(Resource.tags))
        .where(*filters)
        .order_by(Resource.id.asc())
        .offset(offset)
        .limit(limit)
    ).all()

    payload = PaginatedResources(
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + len(rows)) < total,
        items=[ResourceOut.from_model(row) for row in rows],
    )
    body = payload.model_dump_json().encode("utf-8")
    etag = build_etag(body)
    headers = {
        "Cache-Control": PUBLIC_CACHE_CONTROL,
        "ETag": etag,
    }

    if _etag_matches(request, etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)

    return Response(content=body, media_type="application/json", headers=headers)


@router.get("/{resource_id}", response_model=ResourceOut, name="get_resource")
def get_resource(
    resource_id: int,
    request: Request,
    format: Literal["json", "xml", "jsonld"] | None = Query(
        default=None,
        description="Override format: json, xml (Dublin Core), or jsonld (Schema.org).",
    ),
    db: Session = Depends(get_db),
) -> Response:
    resource = _load_resource_or_404(db, resource_id)
    resource_out = ResourceOut.from_model(resource)
    canonical_url = str(request.url_for("get_resource", resource_id=resource.id))
    chosen = resolve_response_format(format, request.headers.get("accept"))

    if chosen == "xml":
        body = serialize_resource_dublin_core_xml(resource_out, canonical_url)
        media_type = "application/xml; charset=utf-8"
    elif chosen == "jsonld":
        data = serialize_resource_jsonld(resource_out, canonical_url)
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        media_type = "application/ld+json; charset=utf-8"
    else:
        body = resource_out.model_dump_json().encode("utf-8")
        media_type = "application/json"

    etag = build_etag(body)
    headers = {
        "Cache-Control": PUBLIC_CACHE_CONTROL,
        "ETag": etag,
        "Vary": "Accept",
    }

    if _etag_matches(request, etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)

    return Response(content=body, media_type=media_type, headers=headers)


@router.post("", response_model=ResourceOut, status_code=status.HTTP_201_CREATED)
def create_resource(
    payload: ResourceCreate,
    response: Response,
    _librarian: User = Depends(require_librarian),
    db: Session = Depends(get_db),
) -> ResourceOut:
    response.headers["Cache-Control"] = "no-store"

    existing = db.scalar(select(Resource).where(Resource.isbn == payload.isbn))
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A resource with ISBN {payload.isbn} already exists.",
        )

    resource = Resource(
        isbn=payload.isbn,
        title=payload.title,
        summary=payload.summary,
        publication_year=payload.publication_year,
        publisher=payload.publisher,
        language=payload.language,
        source_url=payload.source_url,
        total_copies=payload.total_copies,
        available_copies=payload.total_copies,
        authors=_get_or_create_authors(db, payload.authors),
        tags=_get_or_create_tags(db, payload.tags),
    )
    db.add(resource)
    db.commit()
    return ResourceOut.from_model(_load_resource_or_404(db, resource.id))


@router.patch("/{resource_id}", response_model=ResourceOut)
def update_resource(
    resource_id: int,
    payload: ResourceUpdate,
    response: Response,
    _librarian: User = Depends(require_librarian),
    db: Session = Depends(get_db),
) -> ResourceOut:
    response.headers["Cache-Control"] = "no-store"
    resource = _load_resource_or_404(db, resource_id)
    updates = payload.model_dump(exclude_unset=True)

    if "total_copies" in updates and updates["total_copies"] is not None:
        new_total = updates.pop("total_copies")
        borrowed_count = resource.total_copies - resource.available_copies
        if new_total < borrowed_count:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"total_copies ({new_total}) cannot be less than active loans ({borrowed_count}).",
            )
        resource.total_copies = new_total
        resource.available_copies = new_total - borrowed_count

    if "authors" in updates:
        author_names = updates.pop("authors")
        if author_names is not None:
            resource.authors = _get_or_create_authors(db, author_names)

    if "tags" in updates:
        tag_names = updates.pop("tags")
        if tag_names is not None:
            resource.tags = _get_or_create_tags(db, tag_names)

    for field, value in updates.items():
        setattr(resource, field, value)

    db.commit()
    return ResourceOut.from_model(_load_resource_or_404(db, resource.id))


@router.delete("/{resource_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_resource(
    resource_id: int,
    response: Response,
    _librarian: User = Depends(require_librarian),
    db: Session = Depends(get_db),
) -> None:
    response.headers["Cache-Control"] = "no-store"
    resource = _load_resource_or_404(db, resource_id)

    active_loans = db.scalar(
        select(func.count(Loan.id)).where(
            Loan.resource_id == resource.id,
            Loan.status == "active",
        )
    )
    if active_loans:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete a resource while active loans exist.",
        )

    db.delete(resource)
    db.commit()


@router.post("/{resource_id}/verify-link", response_model=LinkVerificationOut)
def verify_resource_link(
    resource_id: int,
    response: Response,
    _librarian: User = Depends(require_librarian),
    db: Session = Depends(get_db),
) -> LinkVerificationOut:
    response.headers["Cache-Control"] = "no-store"
    resource = _load_resource_or_404(db, resource_id)

    if not resource.source_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This resource has no source_url configured.",
        )

    try:
        result = verify_external_url(resource.source_url)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="External reference URL timed out.",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to reach external reference URL.",
        ) from exc

    return LinkVerificationOut(resource_id=resource.id, **result)