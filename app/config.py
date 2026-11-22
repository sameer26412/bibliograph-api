from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    database_url: str = "sqlite:///./bibliograph_local.db"

    secret_key: str = "dev-only-insecure-secret-change-in-env"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    db_connect_timeout_seconds: int = 5
    db_statement_timeout_ms: int = 5000
    outbound_timeout_seconds: float = 3.5

    max_request_body_bytes: int = 65_536
    rate_limit_default_per_minute: int = 60
    rate_limit_auth_per_minute: int = 10

    seed_demo_data: bool = True
    seed_librarian_email: str = "librarian@bibliograph.dev"
    seed_librarian_password: str = "ChangeMeLocal123!"

    @field_validator("secret_key")
    @classmethod
    def validate_secret_key(cls, value: str) -> str:
        if len(value.strip()) < 16:
            raise ValueError("SECRET_KEY must be at least 16 characters long.")
        return value.strip()

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def sqlalchemy_database_url(self) -> str:
        url = self.database_url.strip()
        if url.startswith("postgres://"):
            return url.replace("postgres://", "postgresql+psycopg://", 1)
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+psycopg://", 1)
        return url


settings = Settings()