from datetime import datetime, timedelta, timezone
import math
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.deps import get_current_user, require_librarian
from app.models import ExtensionRequest, Loan, Resource, User, utcnow
from app.schemas import (
    AdminExtensionOut,
    AdminLoanOut,
    ExtensionCreate,
    ExtensionDecision,
    ExtensionOut,
    LoanApprove,
    LoanCreate,
    LoanOut,
    LoanReject,
)

router = APIRouter(prefix="/api/v1/loans", tags=["Circulation & Loans"])


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _load_loan(db: Session, loan_id: int) -> Loan | None:
    return db.scalar(
        select(Loan)
        .options(
            selectinload(Loan.resource),
            selectinload(Loan.user),
            selectinload(Loan.extensions),
        )
        .where(Loan.id == loan_id)
    )


@router.get("/me", response_model=list[LoanOut])
def list_my_loans(
    response: Response,
    loan_status: Literal["pending", "active", "rejected", "returned"] | None = Query(
        default=None, alias="status"
    ),
    limit: int = Query(default=25, ge=1, le=50),
    offset: int = Query(default=0, ge=0, le=10_000),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[LoanOut]:
    response.headers["Cache-Control"] = "no-store"

    stmt = (
        select(Loan)
        .options(selectinload(Loan.resource), selectinload(Loan.extensions))
        .where(Loan.user_id == current_user.id)
    )
    if loan_status is not None:
        stmt = stmt.where(Loan.status == loan_status)

    loans = db.scalars(
        stmt.order_by(Loan.requested_at.desc()).offset(offset).limit(limit)
    ).all()
    return [LoanOut.from_model(loan) for loan in loans]


@router.get("/extensions", response_model=list[AdminExtensionOut])
def list_extension_tickets(
    response: Response,
    ticket_status: Literal["pending", "approved", "declined"] | None = Query(
        default=None, alias="status"
    ),
    limit: int = Query(default=25, ge=1, le=50),
    offset: int = Query(default=0, ge=0, le=10_000),
    _librarian: User = Depends(require_librarian),
    db: Session = Depends(get_db),
) -> list[AdminExtensionOut]:
    response.headers["Cache-Control"] = "no-store"

    stmt = select(ExtensionRequest).options(
        selectinload(ExtensionRequest.user),
        selectinload(ExtensionRequest.loan).selectinload(Loan.resource),
    )
    if ticket_status is not None:
        stmt = stmt.where(ExtensionRequest.status == ticket_status)

    tickets = db.scalars(
        stmt.order_by(ExtensionRequest.requested_at.desc()).offset(offset).limit(limit)
    ).all()
    return [AdminExtensionOut.from_model(ticket) for ticket in tickets]


@router.post("/extensions/{extension_id}/decide", response_model=AdminExtensionOut)
def decide_extension_ticket(
    extension_id: int,
    payload: ExtensionDecision,
    response: Response,
    _librarian: User = Depends(require_librarian),
    db: Session = Depends(get_db),
) -> AdminExtensionOut:
    response.headers["Cache-Control"] = "no-store"

    ticket = db.scalar(
        select(ExtensionRequest)
        .options(
            selectinload(ExtensionRequest.user),
            selectinload(ExtensionRequest.loan).selectinload(Loan.resource),
        )
        .where(ExtensionRequest.id == extension_id)
    )
    if ticket is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Extension ticket {extension_id} not found.",
        )

    if ticket.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This extension ticket has already been decided.",
        )

    if payload.decision == "approved":
        if ticket.loan.status != "active" or ticket.loan.due_at is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot extend a loan that is no longer active.",
            )
        current_due = _as_utc(ticket.loan.due_at)
        ticket.loan.due_at = current_due + timedelta(days=ticket.extra_days)

    ticket.status = payload.decision
    ticket.admin_note = payload.admin_note
    ticket.decided_at = utcnow()
    db.commit()
    db.refresh(ticket)
    return AdminExtensionOut.from_model(ticket)


@router.get("", response_model=list[AdminLoanOut])
def list_all_loans_for_librarian(
    response: Response,
    loan_status: Literal["pending", "active", "rejected", "returned"] | None = Query(
        default=None, alias="status"
    ),
    limit: int = Query(default=25, ge=1, le=50),
    offset: int = Query(default=0, ge=0, le=10_000),
    _librarian: User = Depends(require_librarian),
    db: Session = Depends(get_db),
) -> list[AdminLoanOut]:
    response.headers["Cache-Control"] = "no-store"

    stmt = select(Loan).options(
        selectinload(Loan.resource),
        selectinload(Loan.user),
        selectinload(Loan.extensions),
    )
    if loan_status is not None:
        stmt = stmt.where(Loan.status == loan_status)

    loans = db.scalars(
        stmt.order_by(Loan.requested_at.desc()).offset(offset).limit(limit)
    ).all()
    return [AdminLoanOut.from_model(loan) for loan in loans]


@router.post("", response_model=LoanOut, status_code=status.HTTP_201_CREATED)
def request_borrow(
    payload: LoanCreate,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> LoanOut:
    response.headers["Cache-Control"] = "no-store"
    now = utcnow()

    suspended_until = _as_utc(current_user.suspended_until)
    if suspended_until is not None and suspended_until > now:
        remaining_days = max(1, math.ceil((suspended_until - now).total_seconds() / 86400))
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Borrowing paused for {remaining_days} more day(s) due to a previous late return "
                f"(eligible again on {suspended_until.strftime('%Y-%m-%d %H:%M UTC')})."
            ),
        )

    resource = db.get(Resource, payload.resource_id)
    if resource is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Resource {payload.resource_id} not found.",
        )

    if resource.available_copies <= 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No copies are currently available to request.",
        )

    existing = db.scalar(
        select(Loan).where(
            Loan.user_id == current_user.id,
            Loan.resource_id == payload.resource_id,
            Loan.status.in_(["pending", "active"]),
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You already have a pending request or active loan for this resource.",
        )

    loan = Loan(
        user_id=current_user.id,
        resource_id=payload.resource_id,
        requested_days=payload.days,
        requested_at=now,
        status="pending",
    )
    db.add(loan)
    db.commit()

    loaded = _load_loan(db, loan.id)
    return LoanOut.from_model(loaded)


@router.post("/{loan_id}/approve", response_model=AdminLoanOut)
def approve_and_issue_loan(
    loan_id: int,
    payload: LoanApprove | None = None,
    response: Response = None,
    _librarian: User = Depends(require_librarian),
    db: Session = Depends(get_db),
) -> AdminLoanOut:
    if response is not None:
        response.headers["Cache-Control"] = "no-store"

    loan = _load_loan(db, loan_id)
    if loan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Loan request {loan_id} not found.",
        )

    if loan.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only pending borrow requests can be approved.",
        )

    issued_days = payload.days if (payload and payload.days is not None) else loan.requested_days

    # Atomic stock check + decrement when admin physically issues the book
    decrement = db.execute(
        update(Resource)
        .where(
            Resource.id == loan.resource_id,
            Resource.available_copies > 0,
        )
        .values(available_copies=Resource.available_copies - 1)
    )
    if decrement.rowcount == 0:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot issue: 0 copies left in stock.",
        )

    now = utcnow()
    loan.status = "active"
    loan.requested_days = issued_days
    loan.borrowed_at = now
    loan.due_at = now + timedelta(days=issued_days)
    if payload and payload.admin_note is not None:
        loan.admin_note = payload.admin_note

    db.commit()
    return AdminLoanOut.from_model(_load_loan(db, loan.id))


@router.post("/{loan_id}/reject", response_model=AdminLoanOut)
def reject_loan_request(
    loan_id: int,
    payload: LoanReject | None = None,
    response: Response = None,
    _librarian: User = Depends(require_librarian),
    db: Session = Depends(get_db),
) -> AdminLoanOut:
    if response is not None:
        response.headers["Cache-Control"] = "no-store"

    loan = _load_loan(db, loan_id)
    if loan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Loan request {loan_id} not found.",
        )

    if loan.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only pending borrow requests can be declined.",
        )

    loan.status = "rejected"
    if payload and payload.admin_note is not None:
        loan.admin_note = payload.admin_note

    db.commit()
    return AdminLoanOut.from_model(_load_loan(db, loan.id))


@router.post("/{loan_id}/return", response_model=AdminLoanOut)
def mark_loan_returned(
    loan_id: int,
    response: Response,
    _librarian: User = Depends(require_librarian),
    db: Session = Depends(get_db),
) -> AdminLoanOut:
    response.headers["Cache-Control"] = "no-store"

    loan = _load_loan(db, loan_id)
    if loan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Loan {loan_id} not found.",
        )

    if loan.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only active issued loans can be marked returned.",
        )

    now = utcnow()
    due_at = _as_utc(loan.due_at)
    overdue_days = 0

    if due_at is not None and now > due_at:
        seconds_late = (now - due_at).total_seconds()
        overdue_days = max(1, math.ceil(seconds_late / 86400))

        existing_suspension = _as_utc(loan.user.suspended_until)
        penalty_start = (
            existing_suspension
            if existing_suspension is not None and existing_suspension > now
            else now
        )
        loan.user.suspended_until = penalty_start + timedelta(days=overdue_days)

    loan.status = "returned"
    loan.returned_at = now
    loan.overdue_days = overdue_days

    # Close any open extension tickets on this loan
    for ext in loan.extensions:
        if ext.status == "pending":
            ext.status = "declined"
            ext.decided_at = now
            ext.admin_note = "Closed automatically when book was returned."

    db.execute(
        update(Resource)
        .where(
            Resource.id == loan.resource_id,
            Resource.available_copies < Resource.total_copies,
        )
        .values(available_copies=Resource.available_copies + 1)
    )
    db.commit()
    return AdminLoanOut.from_model(_load_loan(db, loan.id))


@router.post("/{loan_id}/extensions", response_model=ExtensionOut, status_code=status.HTTP_201_CREATED)
def request_loan_extension(
    loan_id: int,
    payload: ExtensionCreate,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ExtensionOut:
    response.headers["Cache-Control"] = "no-store"

    loan = _load_loan(db, loan_id)
    if loan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Loan {loan_id} not found.",
        )

    if loan.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only request extensions for your own active loans.",
        )

    if loan.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Extensions can only be requested on active issued loans.",
        )

    has_pending = any(ext.status == "pending" for ext in loan.extensions)
    if has_pending:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You already have a pending extension ticket for this loan.",
        )

    ticket = ExtensionRequest(
        loan_id=loan.id,
        user_id=current_user.id,
        extra_days=payload.extra_days,
        reason=payload.reason,
        status="pending",
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ExtensionOut.model_validate(ticket)