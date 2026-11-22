from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import settings


def _build_engine():
    db_url = settings.sqlalchemy_database_url

    if settings.is_sqlite:
        engine = create_engine(
            db_url,
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine

    # Serverless PostgreSQL (Neon -pooler / PgBouncer transaction mode):
    # Disable ORM pooling so each invocation opens a fast pooled PgBouncer
    # connection and releases it immediately.
    connect_args: dict[str, object] = {
        "connect_timeout": settings.db_connect_timeout_seconds,
        "prepare_threshold": None,
    }
    if "sslmode=" not in db_url and settings.is_production:
        connect_args["sslmode"] = "require"

    return create_engine(
        db_url,
        poolclass=NullPool,
        connect_args=connect_args,
    )


engine = _build_engine()

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)


@event.listens_for(SessionLocal, "after_begin")
def _apply_postgres_statement_timeout(_session, _transaction, connection):
    if connection.dialect.name == "postgresql":
        timeout_ms = int(settings.db_statement_timeout_ms)
        connection.exec_driver_sql(f"SET LOCAL statement_timeout = {timeout_ms}")


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()