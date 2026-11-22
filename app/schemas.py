from datetime import datetime
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.models import ExtensionRequest, Loan, PasswordResetRequest, Resource, WaitlistEntry
from app.security import validate_public_http_url

ISBN_PATTERN = re.compile(r"^[0-9Xx-]{10,32}$")
PHONE_PATTERN = re.compile(r"^[0-9+\-\s()]{7,25}$")
LICENSE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,40}$")


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _check_password_bytes(value: str) -> str:
    if len(value.encode("utf-8")) > 72:
        raise ValueError("Password cannot exceed 72 UTF-8 bytes.")
    return value


def _validate_clean_list(
    values: list[str],
    *,
    field_name: str,
    min_items: int,
    max_items: int,
    max_item_len: int,
    lowercase: bool = False,
) -> list[str]:
    if len(values) < min_items:
        raise ValueError(f"{field_name} must contain at least {min_items} item(s).")
    if len(values) > max_items:
        raise ValueError(f"{field_name} cannot exceed {max_items} items.")

    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in values:
        item = raw.strip()
        if not item:
            continue
        if len(item) > max_item_len:
            raise ValueError(f"Each item in {field_name} must be at most {max_item_len} characters.")
        normalized = item.lower() if lowercase else item
        key = normalized.lower()
        if key not in seen:
            seen.add(key)
            cleaned.append(normalized)

    if len(cleaned) < min_items:
        raise ValueError(f"{field_name} must contain at least {min_items} non-empty item(s).")
    return cleaned


class UserRegister(StrictSchema):
    email: EmailStr
    full_name: str = Field(min_length=2, max_length=120)
    password: str = Field(min_length=8, max_length=72)
    phone: str = Field(min_length=7, max_length=25)
    address: str = Field(min_length=5, max_length=300)
    member_type: Literal["student", "local"] = "student"
    semester: str | None = Field(default=None, max_length=30)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr) -> str:
        return str(value).lower()

    @field_validator("password")
    @classmethod
    def check_password_bytes(cls, value: str) -> str:
        return _check_password_bytes(value)

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        if not PHONE_PATTERN.match(value):
            raise ValueError("Phone number may contain only digits, spaces, +, -, and parentheses.")
        return value

    @model_validator(mode="after")
    def validate_student_semester(self) -> "UserRegister":
        if self.member_type == "student":
            if not self.semester or not self.semester.strip():
                raise ValueError("Semester is required for college student accounts.")
        else:
            self.semester = None
        return self


class UserLogin(StrictSchema):
    email: EmailStr
    password: str = Field(min_length=1, max_length=72)
    remember_me: bool = False

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr) -> str:
        return str(value).lower()


class ProfileUpdate(StrictSchema):
    full_name: str | None = Field(default=None, min_length=2, max_length=120)
    phone: str | None = Field(default=None, min_length=7, max_length=25)
    address: str | None = Field(default=None, min_length=5, max_length=300)
    semester: str | None = Field(default=None, max_length=30)

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str | None) -> str | None:
        if value is not None and not PHONE_PATTERN.match(value):
            raise ValueError("Phone number may contain only digits, spaces, +, -, and parentheses.")
        return value


class PasswordChange(StrictSchema):
    current_password: str = Field(min_length=1, max_length=72)
    new_password: str = Field(min_length=8, max_length=72)

    @field_validator("new_password")
    @classmethod
    def check_new_password(cls, value: str) -> str:
        return _check_password_bytes(value)


class PasswordResetRequestCreate(StrictSchema):
    email: EmailStr
    phone: str = Field(min_length=7, max_length=25)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr) -> str:
        return str(value).lower()


class PasswordResetConfirm(StrictSchema):
    email: EmailStr
    code: str = Field(min_length=6, max_length=12)
    new_password: str = Field(min_length=8, max_length=72)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr) -> str:
        return str(value).lower()

    @field_validator("new_password")
    @classmethod
    def check_new_password(cls, value: str) -> str:
        return _check_password_bytes(value)


class AdminPasswordResetOut(BaseModel):
    id: int
    user_id: int
    user_name: str
    user_email: str
    user_phone: str
    member_type: str
    status: str
    requested_at: datetime
    expires_at: datetime | None
    issued_by_name: str | None
    issued_by_license: str | None

    @classmethod
    def from_model(cls, req: PasswordResetRequest) -> "AdminPasswordResetOut":
        return cls(
            id=req.id,
            user_id=req.user_id,
            user_name=req.user.full_name,
            user_email=req.user.email,
            user_phone=req.user.phone,
            member_type=req.user.member_type,
            status=req.status,
            requested_at=req.requested_at,
            expires_at=req.expires_at,
            issued_by_name=req.issued_by.full_name if req.issued_by else None,
            issued_by_license=req.issued_by.staff_license_id if req.issued_by else None,
        )


class PasswordResetCodeIssuedOut(AdminPasswordResetOut):
    recovery_code: str


class StaffCreate(StrictSchema):
    email: EmailStr
    full_name: str = Field(min_length=2, max_length=120)
    password: str = Field(min_length=8, max_length=72)
    phone: str = Field(min_length=7, max_length=25)
    address: str = Field(min_length=5, max_length=300)
    staff_license_id: str = Field(min_length=3, max_length=40)
    shift: Literal["Morning", "Afternoon", "Evening", "Full Day"] = "Morning"

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr) -> str:
        return str(value).lower()

    @field_validator("password")
    @classmethod
    def check_password_bytes(cls, value: str) -> str:
        return _check_password_bytes(value)

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        if not PHONE_PATTERN.match(value):
            raise ValueError("Phone number may contain only digits, spaces, +, -, and parentheses.")
        return value

    @field_validator("staff_license_id")
    @classmethod
    def validate_license(cls, value: str) -> str:
        cleaned = value.strip().upper()
        if not LICENSE_PATTERN.match(cleaned):
            raise ValueError("License ID must be 3-40 alphanumeric characters, hyphens, or underscores.")
        return cleaned


class StaffUpdate(StrictSchema):
    shift: Literal["Morning", "Afternoon", "Evening", "Full Day"] | None = None
    is_active: bool | None = None
    phone: str | None = Field(default=None, min_length=7, max_length=25)
    address: str | None = Field(default=None, min_length=5, max_length=300)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    full_name: str
    phone: str
    address: str
    member_type: str
    semester: str | None
    staff_license_id: str | None
    shift: str | None
    is_head_librarian: bool
    is_active: bool
    role: str
    suspended_until: datetime | None
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


class ResourceCreate(StrictSchema):
    isbn: str = Field(min_length=10, max_length=32)
    title: str = Field(min_length=1, max_length=255)
    summary: str = Field(default="", max_length=2000)
    publication_year: int = Field(ge=1450, le=2100)
    publisher: str = Field(min_length=1, max_length=150)
    language: str = Field(default="en", min_length=2, max_length=10)
    source_url: str | None = Field(default=None, max_length=500)
    total_copies: int = Field(default=1, ge=1, le=500)
    authors: list[str]
    tags: list[str] = Field(default_factory=list)

    @field_validator("isbn")
    @classmethod
    def validate_isbn(cls, value: str) -> str:
        cleaned = value.strip().upper()
        if not ISBN_PATTERN.match(cleaned) or len(cleaned.replace("-", "")) < 10:
            raise ValueError("ISBN must contain 10-32 digits/X/hyphens (at least 10 alphanumeric chars).")
        return cleaned

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        return validate_public_http_url(value, resolve_dns=False)

    @field_validator("authors")
    @classmethod
    def validate_authors(cls, value: list[str]) -> list[str]:
        return _validate_clean_list(
            value,
            field_name="authors",
            min_items=1,
            max_items=10,
            max_item_len=120,
        )

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str]) -> list[str]:
        return _validate_clean_list(
            value,
            field_name="tags",
            min_items=0,
            max_items=10,
            max_item_len=60,
            lowercase=True,
        )


class ResourceUpdate(StrictSchema):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    summary: str | None = Field(default=None, max_length=2000)
    publication_year: int | None = Field(default=None, ge=1450, le=2100)
    publisher: str | None = Field(default=None, min_length=1, max_length=150)
    language: str | None = Field(default=None, min_length=2, max_length=10)
    source_url: str | None = Field(default=None, max_length=500)
    total_copies: int | None = Field(default=None, ge=1, le=500)
    authors: list[str] | None = None
    tags: list[str] | None = None

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        return validate_public_http_url(value, resolve_dns=False)

    @field_validator("authors")
    @classmethod
    def validate_authors(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return _validate_clean_list(
            value,
            field_name="authors",
            min_items=1,
            max_items=10,
            max_item_len=120,
        )

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return _validate_clean_list(
            value,
            field_name="tags",
            min_items=0,
            max_items=10,
            max_item_len=60,
            lowercase=True,
        )


class ResourceOut(BaseModel):
    id: int
    isbn: str
    title: str
    summary: str
    publication_year: int
    publisher: str
    language: str
    source_url: str | None
    total_copies: int
    available_copies: int
    authors: list[str]
    tags: list[str]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, resource: Resource) -> "ResourceOut":
        return cls(
            id=resource.id,
            isbn=resource.isbn,
            title=resource.title,
            summary=resource.summary,
            publication_year=resource.publication_year,
            publisher=resource.publisher,
            language=resource.language,
            source_url=resource.source_url,
            total_copies=resource.total_copies,
            available_copies=resource.available_copies,
            authors=sorted(author.name for author in resource.authors),
            tags=sorted(tag.name for tag in resource.tags),
            created_at=resource.created_at,
            updated_at=resource.updated_at,
        )


class PaginatedResources(BaseModel):
    total: int
    limit: int
    offset: int
    has_more: bool
    items: list[ResourceOut]


class LinkVerificationOut(BaseModel):
    resource_id: int
    url: str
    reachable: bool
    status_code: int
    latency_ms: float
    content_type: str | None


class LoanCreate(StrictSchema):
    resource_id: int = Field(gt=0)
    days: int = Field(default=14, ge=1, le=30)


class LoanApprove(StrictSchema):
    days: int | None = Field(default=None, ge=1, le=30)
    admin_note: str | None = Field(default=None, max_length=300)


class LoanReject(StrictSchema):
    admin_note: str | None = Field(default=None, max_length=300)


class ExtensionCreate(StrictSchema):
    extra_days: int = Field(default=7, ge=1, le=14)
    reason: str = Field(min_length=3, max_length=300)


class ExtensionDecision(StrictSchema):
    decision: Literal["approved", "declined"]
    admin_note: str | None = Field(default=None, max_length=300)


class ExtensionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    loan_id: int
    extra_days: int
    reason: str
    status: str
    admin_note: str | None
    requested_at: datetime
    decided_at: datetime | None


class AdminExtensionOut(ExtensionOut):
    borrower_name: str
    borrower_email: str
    borrower_phone: str
    borrower_type: str
    borrower_semester: str | None
    resource_title: str
    resource_isbn: str
    current_due_at: datetime | None
    decided_by_name: str | None
    decided_by_license: str | None

    @classmethod
    def from_model(cls, ext: ExtensionRequest) -> "AdminExtensionOut":
        return cls(
            id=ext.id,
            loan_id=ext.loan_id,
            extra_days=ext.extra_days,
            reason=ext.reason,
            status=ext.status,
            admin_note=ext.admin_note,
            requested_at=ext.requested_at,
            decided_at=ext.decided_at,
            borrower_name=ext.user.full_name,
            borrower_email=ext.user.email,
            borrower_phone=ext.user.phone,
            borrower_type=ext.user.member_type,
            borrower_semester=ext.user.semester,
            resource_title=ext.loan.resource.title,
            resource_isbn=ext.loan.resource.isbn,
            current_due_at=ext.loan.due_at,
            decided_by_name=ext.decided_by.full_name if ext.decided_by else None,
            decided_by_license=ext.decided_by.staff_license_id if ext.decided_by else None,
        )


class LoanOut(BaseModel):
    id: int
    user_id: int
    resource_id: int
    resource_title: str
    resource_isbn: str
    requested_days: int
    requested_at: datetime
    borrowed_at: datetime | None
    due_at: datetime | None
    returned_at: datetime | None
    overdue_days: int
    admin_note: str | None
    status: str
    issued_by_name: str | None
    issued_by_license: str | None
    issued_by_shift: str | None
    returned_by_name: str | None
    returned_by_license: str | None
    extensions: list[ExtensionOut]

    @classmethod
    def from_model(cls, loan: Loan) -> "LoanOut":
        return cls(
            id=loan.id,
            user_id=loan.user_id,
            resource_id=loan.resource_id,
            resource_title=loan.resource.title,
            resource_isbn=loan.resource.isbn,
            requested_days=loan.requested_days,
            requested_at=loan.requested_at,
            borrowed_at=loan.borrowed_at,
            due_at=loan.due_at,
            returned_at=loan.returned_at,
            overdue_days=loan.overdue_days,
            admin_note=loan.admin_note,
            status=loan.status,
            issued_by_name=loan.issued_by.full_name if loan.issued_by else None,
            issued_by_license=loan.issued_by.staff_license_id if loan.issued_by else None,
            issued_by_shift=loan.issued_by.shift if loan.issued_by else None,
            returned_by_name=loan.returned_by.full_name if loan.returned_by else None,
            returned_by_license=loan.returned_by.staff_license_id if loan.returned_by else None,
            extensions=[ExtensionOut.model_validate(ext) for ext in loan.extensions],
        )


class AdminLoanOut(LoanOut):
    borrower_name: str
    borrower_email: str
    borrower_phone: str
    borrower_address: str
    borrower_type: str
    borrower_semester: str | None
    borrower_suspended_until: datetime | None

    @classmethod
    def from_model(cls, loan: Loan) -> "AdminLoanOut":
        return cls(
            id=loan.id,
            user_id=loan.user_id,
            borrower_name=loan.user.full_name,
            borrower_email=loan.user.email,
            borrower_phone=loan.user.phone,
            borrower_address=loan.user.address,
            borrower_type=loan.user.member_type,
            borrower_semester=loan.user.semester,
            borrower_suspended_until=loan.user.suspended_until,
            resource_id=loan.resource_id,
            resource_title=loan.resource.title,
            resource_isbn=loan.resource.isbn,
            requested_days=loan.requested_days,
            requested_at=loan.requested_at,
            borrowed_at=loan.borrowed_at,
            due_at=loan.due_at,
            returned_at=loan.returned_at,
            overdue_days=loan.overdue_days,
            admin_note=loan.admin_note,
            status=loan.status,
            issued_by_name=loan.issued_by.full_name if loan.issued_by else None,
            issued_by_license=loan.issued_by.staff_license_id if loan.issued_by else None,
            issued_by_shift=loan.issued_by.shift if loan.issued_by else None,
            returned_by_name=loan.returned_by.full_name if loan.returned_by else None,
            returned_by_license=loan.returned_by.staff_license_id if loan.returned_by else None,
            extensions=[ExtensionOut.model_validate(ext) for ext in loan.extensions],
        )


class WaitlistCreate(StrictSchema):
    resource_id: int = Field(gt=0)


class WaitlistAssign(StrictSchema):
    note: str | None = Field(default=None, max_length=300)


class WaitlistFulfill(StrictSchema):
    days: int = Field(default=14, ge=1, le=30)
    admin_note: str | None = Field(default=None, max_length=300)


class WaitlistOut(BaseModel):
    id: int
    resource_id: int
    resource_title: str
    resource_isbn: str
    status: str
    expected_available_at: datetime | None
    hold_expires_at: datetime | None
    note: str | None
    assigned_by_name: str | None
    assigned_by_license: str | None
    assigned_by_shift: str | None
    created_at: datetime

    @classmethod
    def from_model(cls, entry: WaitlistEntry) -> "WaitlistOut":
        return cls(
            id=entry.id,
            resource_id=entry.resource_id,
            resource_title=entry.resource.title,
            resource_isbn=entry.resource.isbn,
            status=entry.status,
            expected_available_at=entry.expected_available_at,
            hold_expires_at=entry.hold_expires_at,
            note=entry.note,
            assigned_by_name=entry.assigned_by.full_name if entry.assigned_by else None,
            assigned_by_license=entry.assigned_by.staff_license_id if entry.assigned_by else None,
            assigned_by_shift=entry.assigned_by.shift if entry.assigned_by else None,
            created_at=entry.created_at,
        )


class AdminWaitlistOut(WaitlistOut):
    user_id: int
    borrower_name: str
    borrower_email: str
    borrower_phone: str
    borrower_address: str
    borrower_type: str
    borrower_semester: str | None
    borrower_suspended_until: datetime | None

    @classmethod
    def from_model(cls, entry: WaitlistEntry) -> "AdminWaitlistOut":
        return cls(
            id=entry.id,
            user_id=entry.user_id,
            borrower_name=entry.user.full_name,
            borrower_email=entry.user.email,
            borrower_phone=entry.user.phone,
            borrower_address=entry.user.address,
            borrower_type=entry.user.member_type,
            borrower_semester=entry.user.semester,
            borrower_suspended_until=entry.user.suspended_until,
            resource_id=entry.resource_id,
            resource_title=entry.resource.title,
            resource_isbn=entry.resource.isbn,
            status=entry.status,
            expected_available_at=entry.expected_available_at,
            hold_expires_at=entry.hold_expires_at,
            note=entry.note,
            assigned_by_name=entry.assigned_by.full_name if entry.assigned_by else None,
            assigned_by_license=entry.assigned_by.staff_license_id if entry.assigned_by else None,
            assigned_by_shift=entry.assigned_by.shift if entry.assigned_by else None,
            created_at=entry.created_at,
        )


class ResourceAvailabilityOut(BaseModel):
    resource_id: int
    next_available_at: datetime | None
    waiting_count: int
    ready_holds: int


class HealthOut(BaseModel):
    status: str
    database: str
    environment: str