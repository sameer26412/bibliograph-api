from datetime import datetime, timedelta, timezone
import math
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.deps import get_current_user, require_librarian
from app.models import ExtensionRequest, Loan, Resource, User, WaitlistEntry, utcnow
from app.schemas import (
    AdminExtensionOut,
    AdminLoanOut,
    AdminWaitlistOut,
    ExtensionCreate,
    ExtensionDecision,
    ExtensionOut,
    LoanApprove,
    LoanCreate,
    LoanOut,
    LoanReject,
    ResourceAvailabilityOut,
    WaitlistAssign,
    WaitlistCreate,
    WaitlistFulfill,
    WaitlistOut,
)

router = APIRouter(prefix="/api/v1/loans", tags=["Circulation & Loans"])

WAITLIST_HOLD_DAYS = 3


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
            selectinload(Loan.issued_by),
            selectinload(Loan.returned_by),
            selectinload(Loan.extensions),
        )
        .where(Loan.id == loan_id)
    )


def _load_waitlist_entry(db: Session, entry_id: int) -> WaitlistEntry | None:
    return db.scalar(
        select(WaitlistEntry)
        .options(
            selectinload(WaitlistEntry.resource),
            selectinload(WaitlistEntry.user),
            selectinload(WaitlistEntry.assigned_by),
        )
        .where(WaitlistEntry.id == entry_id)
    )


def _earliest_due_date(db: Session, resource_id: int) -> datetime | None:
    return db.scalar(
        select(func.min(Loan.due_at)).where(
            Loan.resource_id == resource_id,
            Loan.status == "active",
            Loan.due_at.is_not(None),
        )
    )


# --- Public Availability & Expected Return Forecast (Non-PII) ---

@router.get("/availability", response_model=list[ResourceAvailabilityOut])
def get_catalogue_availability_forecast(
    response: Response,
    db: Session = Depends(get_db),
) -> list[ResourceAvailabilityOut]:
    response.headers["Cache-Control"] = "no-store"

    due_rows = db.execute(
        select(Loan.resource_id, func.min(Loan.due_at))
        .where(Loan.status == "active", Loan.due_at.is_not(None))
        .group_by(Loan.resource_id)
    ).all()
    next_due_map = {rid: due for rid, due in due_rows}

    wait_rows = db.execute(
        select(WaitlistEntry.resource_id, func.count(WaitlistEntry.id))
        .where(WaitlistEntry.status.in_(["waiting", "assigned"]))
        .group_by(WaitlistEntry.resource_id)
    ).all()
    wait_map = {rid: cnt for rid, cnt in wait_rows}

    ready_rows = db.execute(
        select(WaitlistEntry.resource_id, func.count(WaitlistEntry.id))
        .where(WaitlistEntry.status == "ready")
        .group_by(WaitlistEntry.resource_id)
    ).all()
    ready_map = {rid: cnt for rid, cnt in ready_rows}

    all_ids = sorted(set(next_due_map) | set(wait_map) | set(ready_map))
    return [
        ResourceAvailabilityOut(
            resource_id=rid,
            next_available_at=next_due_map.get(rid),
            waiting_count=wait_map.get(rid, 0),
            ready_holds=ready_map.get(rid, 0),
        )
        for rid in all_ids
    ]


# --- Rare Book Waitlist & Pre-Assignment ---

@router.post("/waitlist", response_model=WaitlistOut, status_code=status.HTTP_201_CREATED)
def join_waitlist(
    payload: WaitlistCreate,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WaitlistOut:
    response.headers["Cache-Control"] = "no-store"
    now = utcnow()

    suspended_until = _as_utc(current_user.suspended_until)
    if suspended_until is not None and suspended_until > now:
        remaining = max(1, math.ceil((suspended_until - now).total_seconds() / 86400))
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Cannot join waitlist while borrowing is paused ({remaining} day(s) remaining).",
        )

    resource = db.get(Resource, payload.resource_id)
    if resource is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Resource {payload.resource_id} not found.",
        )

    if resource.available_copies > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Copies are currently available—submit a direct borrow request instead.",
        )

    existing_loan = db.scalar(
        select(Loan).where(
            Loan.user_id == current_user.id,
            Loan.resource_id == payload.resource_id,
            Loan.status.in_(["pending", "active"]),
        )
    )
    if existing_loan is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You already hold or have requested this book.",
        )

    existing_wait = db.scalar(
        select(WaitlistEntry).where(
            WaitlistEntry.user_id == current_user.id,
            WaitlistEntry.resource_id == payload.resource_id,
            WaitlistEntry.status.in_(["waiting", "assigned", "ready"]),
        )
    )
    if existing_wait is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You are already on the waitlist for this book.",
        )

    entry = WaitlistEntry(
        resource_id=payload.resource_id,
        user_id=current_user.id,
        status="waiting",
        expected_available_at=_earliest_due_date(db, payload.resource_id),
    )
    db.add(entry)
    db.commit()
    return WaitlistOut.from_model(_load_waitlist_entry(db, entry.id))


@router.get("/waitlist/me", response_model=list[WaitlistOut])
def list_my_waitlist(
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[WaitlistOut]:
    response.headers["Cache-Control"] = "no-store"

    rows = db.scalars(
        select(WaitlistEntry)
        .options(
            selectinload(WaitlistEntry.resource),
            selectinload(WaitlistEntry.assigned_by),
        )
        .where(WaitlistEntry.user_id == current_user.id)
        .order_by(WaitlistEntry.created_at.desc())
        .limit(50)
    ).all()
    return [WaitlistOut.from_model(row) for row in rows]


@router.get("/waitlist", response_model=list[AdminWaitlistOut])
def list_all_waitlist_for_librarian(
    response: Response,
    entry_status: Literal["waiting", "assigned", "ready", "fulfilled", "cancelled"] | None = Query(
        default=None, alias="status"
    ),
    _librarian: User = Depends(require_librarian),
    db: Session = Depends(get_db),
) -> list[AdminWaitlistOut]:
    response.headers["Cache-Control"] = "no-store"

    stmt = select(WaitlistEntry).options(
        selectinload(WaitlistEntry.resource),
        selectinload(WaitlistEntry.user),
        selectinload(WaitlistEntry.assigned_by),
    )
    if entry_status is not None:
        stmt = stmt.where(WaitlistEntry.status == entry_status)

    rows = db.scalars(
        stmt.order_by(WaitlistEntry.created_at.asc()).limit(50)
    ).all()
    return [AdminWaitlistOut.from_model(row) for row in rows]


@router.post("/waitlist/{entry_id}/assign", response_model=AdminWaitlistOut)
def pre_assign_waitlist_spot(
    entry_id: int,
    payload: WaitlistAssign | None = None,
    response: Response = None,
    librarian: User = Depends(require_librarian),
    db: Session = Depends(get_db),
) -> AdminWaitlistOut:
    if response is not None:
        response.headers["Cache-Control"] = "no-store"

    entry = _load_waitlist_entry(db, entry_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Waitlist entry {entry_id} not found.",
        )

    if entry.status != "waiting":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only waiting entries can be pre-assigned.",
        )

    expected = _earliest_due_date(db, entry.resource_id)
    entry.status = "assigned"
    entry.assigned_by_id = librarian.id
    entry.expected_available_at = expected
    if payload and payload.note:
        entry.note = payload.note
    else:
        when = expected.strftime("%Y-%m-%d") if expected else "the next return"
        entry.note = (
            f"Spot secured by {librarian.full_name} ({librarian.staff_license_id or 'Staff'}). "
            f"Expected return: {when}. You will be notified the moment it arrives."
        )

    db.commit()
    db.expire_all()
    return AdminWaitlistOut.from_model(_load_waitlist_entry(db, entry.id))


@router.post("/waitlist/{entry_id}/fulfill", response_model=AdminLoanOut)
def issue_ready_waitlist_hold(
    entry_id: int,
    payload: WaitlistFulfill | None = None,
    response: Response = None,
    librarian: User = Depends(require_librarian),
    db: Session = Depends(get_db),
) -> AdminLoanOut:
    if response is not None:
        response.headers["Cache-Control"] = "no-store"

    entry = _load_waitlist_entry(db, entry_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Waitlist entry {entry_id} not found.",
        )

    days = payload.days if payload else 14
    now = utcnow()
    claimed = db.execute(
        update(WaitlistEntry)
        .where(
            WaitlistEntry.id == entry.id,
            WaitlistEntry.status == "ready",
        )
        .values(
            status="fulfilled",
            note=f"Issued by {librarian.full_name} ({librarian.staff_license_id or 'Staff'}).",
        )
    )
    if claimed.rowcount == 0:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only a 'ready' reserved hold can be issued directly.",
        )

    loan = Loan(
        user_id=entry.user_id,
        resource_id=entry.resource_id,
        issued_by_id=librarian.id,
        requested_days=days,
        requested_at=entry.created_at,
        borrowed_at=now,
        due_at=now + timedelta(days=days),
        admin_note=(payload.admin_note if payload and payload.admin_note else "Issued from reserved waitlist hold."),
        status="active",
    )
    db.add(loan)
    db.commit()
    db.expire_all()
    return AdminLoanOut.from_model(_load_loan(db, loan.id))


@router.delete("/waitlist/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def cancel_waitlist_entry(
    entry_id: int,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    response.headers["Cache-Control"] = "no-store"

    entry = _load_waitlist_entry(db, entry_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Waitlist entry {entry_id} not found.",
        )

    if entry.user_id != current_user.id and current_user.role != "librarian":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You cannot cancel another member's waitlist spot.",
        )

    if entry.status in ("fulfilled", "cancelled"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This waitlist entry is already closed.",
        )

    original_status = entry.status
    claimed = db.execute(
        update(WaitlistEntry)
        .where(
            WaitlistEntry.id == entry.id,
            WaitlistEntry.status == original_status,
            WaitlistEntry.status.not_in(("fulfilled", "cancelled")),
        )
        .values(status="cancelled")
    )
    if claimed.rowcount == 0:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This waitlist entry is already closed.",
        )

    if original_status == "ready":
        promoted = False
        candidates = db.scalars(
            select(WaitlistEntry)
            .where(
                WaitlistEntry.resource_id == entry.resource_id,
                WaitlistEntry.status == "assigned",
                WaitlistEntry.id != entry.id,
            )
            .order_by(WaitlistEntry.created_at.asc())
        ).all()
        for next_assigned in candidates:
            hold_until = utcnow() + timedelta(days=WAITLIST_HOLD_DAYS)
            hold_note = (
                f"A copy of '{entry.resource.title}' is now held exclusively for you "
                f"until {hold_until.strftime('%Y-%m-%d %H:%M UTC')}."
            )
            res = db.execute(
                update(WaitlistEntry)
                .where(
                    WaitlistEntry.id == next_assigned.id,
                    WaitlistEntry.status == "assigned",
                )
                .values(status="ready", hold_expires_at=hold_until, note=hold_note)
            )
            if res.rowcount == 1:
                promoted = True
                break

        if not promoted:
            db.execute(
                update(Resource)
                .where(
                    Resource.id == entry.resource_id,
                    Resource.available_copies < Resource.total_copies,
                )
                .values(available_copies=Resource.available_copies + 1)
            )

    db.commit()


# --- Member Activity & Extension Tickets ---

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
        .options(
            selectinload(Loan.resource),
            selectinload(Loan.issued_by),
            selectinload(Loan.returned_by),
            selectinload(Loan.extensions),
        )
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
        selectinload(ExtensionRequest.decided_by),
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
    librarian: User = Depends(require_librarian),
    db: Session = Depends(get_db),
) -> AdminExtensionOut:
    response.headers["Cache-Control"] = "no-store"

    ticket = db.scalar(
        select(ExtensionRequest)
        .options(
            selectinload(ExtensionRequest.user),
            selectinload(ExtensionRequest.decided_by),
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
    ticket.decided_by_id = librarian.id
    ticket.decided_at = utcnow()
    db.commit()
    db.expire_all()

    reloaded = db.scalar(
        select(ExtensionRequest)
        .options(
            selectinload(ExtensionRequest.user),
            selectinload(ExtensionRequest.decided_by),
            selectinload(ExtensionRequest.loan).selectinload(Loan.resource),
        )
        .where(ExtensionRequest.id == ticket.id)
    )
    return AdminExtensionOut.from_model(reloaded)


# --- Core Circulation Endpoints ---

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
        selectinload(Loan.issued_by),
        selectinload(Loan.returned_by),
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
            detail="No copies are currently available. Join the waitlist to reserve the next return.",
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
    return LoanOut.from_model(_load_loan(db, loan.id))


@router.post("/{loan_id}/approve", response_model=AdminLoanOut)
def approve_and_issue_loan(
    loan_id: int,
    payload: LoanApprove | None = None,
    response: Response = None,
    librarian: User = Depends(require_librarian),
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

    issued_days = payload.days if (payload and payload.days is not None) else loan.requested_days
    now = utcnow()
    transition_values = {
        "status": "active",
        "issued_by_id": librarian.id,
        "requested_days": issued_days,
        "borrowed_at": now,
        "due_at": now + timedelta(days=issued_days),
    }
    if payload and payload.admin_note is not None:
        transition_values["admin_note"] = payload.admin_note

    claimed = db.execute(
        update(Loan)
        .where(
            Loan.id == loan.id,
            Loan.status == "pending",
        )
        .values(**transition_values)
    )
    if claimed.rowcount == 0:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only pending borrow requests can be approved.",
        )

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

    db.commit()
    db.expire_all()
    return AdminLoanOut.from_model(_load_loan(db, loan.id))


@router.post("/{loan_id}/reject", response_model=AdminLoanOut)
def reject_loan_request(
    loan_id: int,
    payload: LoanReject | None = None,
    response: Response = None,
    librarian: User = Depends(require_librarian),
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

    values = {
        "status": "rejected",
        "issued_by_id": librarian.id,
    }
    if payload and payload.admin_note is not None:
        values["admin_note"] = payload.admin_note

    claimed = db.execute(
        update(Loan)
        .where(
            Loan.id == loan.id,
            Loan.status == "pending",
        )
        .values(**values)
    )
    if claimed.rowcount == 0:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only pending borrow requests can be declined.",
        )

    db.commit()
    db.expire_all()
    return AdminLoanOut.from_model(_load_loan(db, loan.id))


@router.post("/{loan_id}/return", response_model=AdminLoanOut)
def mark_loan_returned(
    loan_id: int,
    response: Response,
    librarian: User = Depends(require_librarian),
    db: Session = Depends(get_db),
) -> AdminLoanOut:
    response.headers["Cache-Control"] = "no-store"

    loan = _load_loan(db, loan_id)
    if loan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Loan {loan_id} not found.",
        )

    now = utcnow()
    due_at = _as_utc(loan.due_at)
    overdue_days = 0
    if due_at is not None and now > due_at:
        seconds_late = (now - due_at).total_seconds()
        overdue_days = max(1, math.ceil(seconds_late / 86400))

    claimed = db.execute(
        update(Loan)
        .where(
            Loan.id == loan.id,
            Loan.status == "active",
        )
        .values(
            status="returned",
            returned_at=now,
            returned_by_id=librarian.id,
            overdue_days=overdue_days,
        )
    )
    if claimed.rowcount == 0:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only active issued loans can be marked returned.",
        )

    if overdue_days > 0:
        existing_suspension = _as_utc(loan.user.suspended_until)
        penalty_start = (
            existing_suspension
            if existing_suspension is not None and existing_suspension > now
            else now
        )
        loan.user.suspended_until = penalty_start + timedelta(days=overdue_days)

    for ext in loan.extensions:
        if ext.status == "pending":
            ext.status = "declined"
            ext.decided_by_id = librarian.id
            ext.decided_at = now
            ext.admin_note = "Closed automatically when book was returned."

    # Atomically promote the earliest pre-assigned waitlist reader; otherwise return to public stock
    promoted = False
    candidates = db.scalars(
        select(WaitlistEntry)
        .where(
            WaitlistEntry.resource_id == loan.resource_id,
            WaitlistEntry.status == "assigned",
        )
        .order_by(WaitlistEntry.created_at.asc())
    ).all()
    for candidate in candidates:
        hold_until = now + timedelta(days=WAITLIST_HOLD_DAYS)
        hold_note = (
            f"Your reserved copy of '{loan.resource.title}' has arrived! "
            f"Held exclusively for you until {hold_until.strftime('%Y-%m-%d %H:%M UTC')}."
        )
        promote_res = db.execute(
            update(WaitlistEntry)
            .where(
                WaitlistEntry.id == candidate.id,
                WaitlistEntry.status == "assigned",
            )
            .values(status="ready", hold_expires_at=hold_until, note=hold_note)
        )
        if promote_res.rowcount == 1:
            promoted = True
            break

    if not promoted:
        db.execute(
            update(Resource)
            .where(
                Resource.id == loan.resource_id,
                Resource.available_copies < Resource.total_copies,
            )
            .values(available_copies=Resource.available_copies + 1)
        )

    db.commit()
    db.expire_all()
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

    if any(ext.status == "pending" for ext in loan.extensions):
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