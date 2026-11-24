from datetime import datetime, timedelta, timezone
import re
import secrets
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.database import get_db
from app.deps import get_current_user, require_head_librarian, require_librarian
from app.models import PasswordResetRequest, User, utcnow
from app.schemas import (
    AdminPasswordResetOut,
    PasswordChange,
    PasswordResetCodeIssuedOut,
    PasswordResetConfirm,
    PasswordResetRequestCreate,
    ProfileUpdate,
    StaffCreate,
    StaffUpdate,
    TokenResponse,
    UserLogin,
    UserOut,
    UserRegister,
)
from app.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/api/v1/auth", tags=["Authentication & Profiles"])

REMEMBER_ME_MINUTES = 60 * 24 * 7  # 7 days
RESET_CODE_TTL_MINUTES = 15
MAX_RESET_ATTEMPTS = 5


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _digits(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def _phones_match(submitted: str, stored: str) -> bool:
    a, b = _digits(submitted), _digits(stored)
    if not a or not b:
        return False
    if a == b:
        return True
    return len(a) >= 10 and len(b) >= 10 and a[-10:] == b[-10:]


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register_user(
    payload: UserRegister,
    response: Response,
    db: Session = Depends(get_db),
) -> UserOut:
    response.headers["Cache-Control"] = "no-store"

    existing = db.scalar(select(User).where(User.email == payload.email))
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )

    user = User(
        email=payload.email,
        full_name=payload.full_name,
        phone=payload.phone,
        address=payload.address,
        member_type=payload.member_type,
        semester=payload.semester,
        hashed_password=hash_password(payload.password),
        role="member",
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserOut.model_validate(user)


@router.post("/login", response_model=TokenResponse)
def login_user(
    payload: UserLogin,
    response: Response,
    db: Session = Depends(get_db),
) -> TokenResponse:
    response.headers["Cache-Control"] = "no-store"

    user = db.scalar(select(User).where(User.email == payload.email))
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account has been deactivated. Contact the Head Librarian.",
        )

    ttl_minutes = REMEMBER_ME_MINUTES if payload.remember_me else settings.access_token_expire_minutes
    token = create_access_token(user_id=user.id, role=user.role, expires_minutes=ttl_minutes)
    return TokenResponse(
        access_token=token,
        expires_in=ttl_minutes * 60,
        user=UserOut.model_validate(user),
    )


@router.get("/me", response_model=UserOut)
def read_current_user(
    response: Response,
    current_user: User = Depends(get_current_user),
) -> UserOut:
    response.headers["Cache-Control"] = "no-store"
    return UserOut.model_validate(current_user)


@router.patch("/me", response_model=UserOut)
def update_my_profile(
    payload: ProfileUpdate,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserOut:
    response.headers["Cache-Control"] = "no-store"

    if payload.full_name is not None:
        current_user.full_name = payload.full_name
    if payload.phone is not None:
        current_user.phone = payload.phone
    if payload.address is not None:
        current_user.address = payload.address
    if payload.semester is not None and current_user.member_type == "student":
        current_user.semester = payload.semester

    db.commit()
    db.refresh(current_user)
    return UserOut.model_validate(current_user)


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_my_password(
    payload: PasswordChange,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    response.headers["Cache-Control"] = "no-store"

    if not verify_password(payload.current_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect.",
        )

    current_user.hashed_password = hash_password(payload.new_password)
    db.commit()


# --- Librarian-Verified Password Recovery ---

@router.post("/password-reset/request", status_code=status.HTTP_202_ACCEPTED)
def request_password_reset(
    payload: PasswordResetRequestCreate,
    response: Response,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    response.headers["Cache-Control"] = "no-store"

    user = db.scalar(select(User).where(User.email == payload.email))
    if user is None or not user.is_active or not _phones_match(payload.phone, user.phone):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email and registered phone number did not match an active account.",
        )

    # Cancel earlier uncompleted requests for this account
    open_reqs = db.scalars(
        select(PasswordResetRequest).where(
            PasswordResetRequest.user_id == user.id,
            PasswordResetRequest.status.in_(["pending", "issued"]),
        )
    ).all()
    for req in open_reqs:
        req.status = "cancelled"

    db.add(PasswordResetRequest(user_id=user.id, status="pending"))
    db.commit()
    return {
        "detail": (
            "Password reset request submitted. Ask any on-duty librarian at the desk "
            "for your 15-minute 6-digit recovery code."
        )
    }


@router.get("/password-reset/requests", response_model=list[AdminPasswordResetOut])
def list_password_reset_requests(
    response: Response,
    req_status: Literal["pending", "issued", "completed", "cancelled"] | None = Query(
        default=None, alias="status"
    ),
    _librarian: User = Depends(require_librarian),
    db: Session = Depends(get_db),
) -> list[AdminPasswordResetOut]:
    response.headers["Cache-Control"] = "no-store"

    stmt = select(PasswordResetRequest).options(
        selectinload(PasswordResetRequest.user),
        selectinload(PasswordResetRequest.issued_by),
    )
    if req_status is not None:
        stmt = stmt.where(PasswordResetRequest.status == req_status)

    rows = db.scalars(
        stmt.order_by(PasswordResetRequest.requested_at.desc()).limit(50)
    ).all()
    return [AdminPasswordResetOut.from_model(row) for row in rows]


@router.post("/password-reset/{request_id}/issue", response_model=PasswordResetCodeIssuedOut)
def issue_password_reset_code(
    request_id: int,
    response: Response,
    librarian: User = Depends(require_librarian),
    db: Session = Depends(get_db),
) -> PasswordResetCodeIssuedOut:
    response.headers["Cache-Control"] = "no-store"

    req = db.scalar(
        select(PasswordResetRequest)
        .options(
            selectinload(PasswordResetRequest.user),
            selectinload(PasswordResetRequest.issued_by),
        )
        .where(PasswordResetRequest.id == request_id)
    )
    if req is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Password reset request {request_id} not found.",
        )

    if req.status not in ("pending", "issued"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This password reset request is already closed.",
        )

    plain_code = f"{secrets.randbelow(1_000_000):06d}"
    req.code_hash = hash_password(plain_code)
    req.status = "issued"
    req.issued_by_id = librarian.id
    req.expires_at = utcnow() + timedelta(minutes=RESET_CODE_TTL_MINUTES)
    req.failed_attempts = 0
    db.commit()
    db.expire_all()

    reloaded = db.scalar(
        select(PasswordResetRequest)
        .options(
            selectinload(PasswordResetRequest.user),
            selectinload(PasswordResetRequest.issued_by),
        )
        .where(PasswordResetRequest.id == req.id)
    )
    base = AdminPasswordResetOut.from_model(reloaded)
    return PasswordResetCodeIssuedOut(**base.model_dump(), recovery_code=plain_code)


@router.post("/password-reset/confirm", status_code=status.HTTP_200_OK)
def confirm_password_reset(
    payload: PasswordResetConfirm,
    response: Response,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    response.headers["Cache-Control"] = "no-store"

    user = db.scalar(select(User).where(User.email == payload.email))
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid recovery code or email.",
        )

    req = db.scalar(
        select(PasswordResetRequest)
        .where(
            PasswordResetRequest.user_id == user.id,
            PasswordResetRequest.status == "issued",
        )
        .order_by(PasswordResetRequest.requested_at.desc())
    )
    now = utcnow()
    if (
        req is None
        or not req.code_hash
        or req.expires_at is None
        or _as_utc(req.expires_at) < now
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired recovery code. Request a fresh code from the librarian.",
        )

    if not verify_password(payload.code.strip(), req.code_hash):
        db.execute(
            update(PasswordResetRequest)
            .where(
                PasswordResetRequest.id == req.id,
                PasswordResetRequest.status == "issued",
            )
            .values(failed_attempts=PasswordResetRequest.failed_attempts + 1)
        )
        db.flush()
        db.refresh(req)
        if req.failed_attempts >= MAX_RESET_ATTEMPTS:
            req.status = "cancelled"
            req.code_hash = None
            db.commit()
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Recovery code invalidated after {MAX_RESET_ATTEMPTS} failed attempts. "
                    "Request a fresh code from the librarian."
                ),
            )
        db.commit()
        remaining = MAX_RESET_ATTEMPTS - req.failed_attempts
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid recovery code. {remaining} attempt(s) remaining before lockout.",
        )

    claimed = db.execute(
        update(PasswordResetRequest)
        .where(
            PasswordResetRequest.id == req.id,
            PasswordResetRequest.status == "issued",
            PasswordResetRequest.failed_attempts < MAX_RESET_ATTEMPTS,
        )
        .values(status="completed", completed_at=now, code_hash=None)
    )
    if claimed.rowcount == 0:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Recovery code has already been used or invalidated.",
        )

    user.hashed_password = hash_password(payload.new_password)
    db.commit()
    return {"detail": "Password reset complete. You can now sign in with your new password."}


# --- Multi-Librarian Shift Profiles (Head Librarian Governance) ---

@router.get("/staff", response_model=list[UserOut])
def list_librarian_staff(
    response: Response,
    _librarian: User = Depends(require_librarian),
    db: Session = Depends(get_db),
) -> list[UserOut]:
    response.headers["Cache-Control"] = "no-store"

    staff = db.scalars(
        select(User)
        .where(User.role == "librarian")
        .order_by(User.is_head_librarian.desc(), User.created_at.asc())
    ).all()
    return [UserOut.model_validate(s) for s in staff]


@router.post("/staff", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_shift_librarian(
    payload: StaffCreate,
    response: Response,
    _head: User = Depends(require_head_librarian),
    db: Session = Depends(get_db),
) -> UserOut:
    response.headers["Cache-Control"] = "no-store"

    if db.scalar(select(User).where(User.email == payload.email)) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this email already exists.",
        )

    if db.scalar(select(User).where(User.staff_license_id == payload.staff_license_id)) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Staff license ID '{payload.staff_license_id}' is already assigned.",
        )

    staff = User(
        email=payload.email,
        full_name=payload.full_name,
        phone=payload.phone,
        address=payload.address,
        member_type="local",
        semester=None,
        staff_license_id=payload.staff_license_id,
        shift=payload.shift,
        is_head_librarian=False,
        is_active=True,
        hashed_password=hash_password(payload.password),
        role="librarian",
    )
    db.add(staff)
    db.commit()
    db.refresh(staff)
    return UserOut.model_validate(staff)


@router.patch("/staff/{staff_id}", response_model=UserOut)
def update_shift_librarian(
    staff_id: int,
    payload: StaffUpdate,
    response: Response,
    head: User = Depends(require_head_librarian),
    db: Session = Depends(get_db),
) -> UserOut:
    response.headers["Cache-Control"] = "no-store"

    staff = db.get(User, staff_id)
    if staff is None or staff.role != "librarian":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Librarian staff account {staff_id} not found.",
        )

    if staff.id == head.id and payload.is_active is False:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The Head Librarian cannot deactivate their own account.",
        )

    if payload.shift is not None:
        staff.shift = payload.shift
    if payload.is_active is not None:
        staff.is_active = payload.is_active
    if payload.phone is not None:
        staff.phone = payload.phone
    if payload.address is not None:
        staff.address = payload.address

    db.commit()
    db.refresh(staff)
    return UserOut.model_validate(staff)