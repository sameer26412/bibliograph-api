from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


resource_authors = Table(
    "resource_authors",
    Base.metadata,
    Column(
        "resource_id",
        ForeignKey("resources.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "author_id",
        ForeignKey("authors.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)

resource_tags = Table(
    "resource_tags",
    Base.metadata,
    Column(
        "resource_id",
        ForeignKey("resources.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "tag_id",
        ForeignKey("tags.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    phone: Mapped[str] = mapped_column(String(25), nullable=False, default="")
    address: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    member_type: Mapped[str] = mapped_column(String(20), nullable=False, default="local")
    semester: Mapped[str | None] = mapped_column(String(30), nullable=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="member")
    suspended_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    loans: Mapped[list["Loan"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )


class Author(Base):
    __tablename__ = "authors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)

    resources: Mapped[list["Resource"]] = relationship(
        secondary=resource_authors,
        back_populates="authors",
    )


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(60), unique=True, index=True, nullable=False)

    resources: Mapped[list["Resource"]] = relationship(
        secondary=resource_tags,
        back_populates="tags",
    )


class Resource(Base):
    __tablename__ = "resources"
    __table_args__ = (
        CheckConstraint("total_copies >= 1", name="ck_resources_total_copies_min"),
        CheckConstraint("available_copies >= 0", name="ck_resources_available_min"),
        CheckConstraint("available_copies <= total_copies", name="ck_resources_available_lte_total"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    isbn: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    publication_year: Mapped[int] = mapped_column(Integer, nullable=False)
    publisher: Mapped[str] = mapped_column(String(150), nullable=False)
    language: Mapped[str] = mapped_column(String(10), nullable=False, default="en")
    source_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    total_copies: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    available_copies: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )

    authors: Mapped[list["Author"]] = relationship(
        secondary=resource_authors,
        back_populates="resources",
    )
    tags: Mapped[list["Tag"]] = relationship(
        secondary=resource_tags,
        back_populates="resources",
    )
    loans: Mapped[list["Loan"]] = relationship(
        back_populates="resource",
        cascade="all, delete-orphan",
    )


class Loan(Base):
    __tablename__ = "loans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    resource_id: Mapped[int] = mapped_column(
        ForeignKey("resources.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    requested_days: Mapped[int] = mapped_column(Integer, nullable=False, default=14)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    borrowed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    returned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    overdue_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    admin_note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # State machine: pending -> active -> returned, or pending -> rejected
    status: Mapped[str] = mapped_column(String(20), index=True, nullable=False, default="pending")

    user: Mapped["User"] = relationship(back_populates="loans")
    resource: Mapped["Resource"] = relationship(back_populates="loans")
    extensions: Mapped[list["ExtensionRequest"]] = relationship(
        back_populates="loan",
        cascade="all, delete-orphan",
        order_by="ExtensionRequest.requested_at.desc()",
    )


class ExtensionRequest(Base):
    __tablename__ = "extension_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    loan_id: Mapped[int] = mapped_column(
        ForeignKey("loans.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    extra_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    reason: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(20), index=True, nullable=False, default="pending")
    admin_note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    loan: Mapped["Loan"] = relationship(back_populates="extensions")
    user: Mapped["User"] = relationship()