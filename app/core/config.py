from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "MediLink API"
    app_env: str = "local"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    firebase_project_id: str | None = None
    private_upload_dir: str = "./data/private_uploads"
    cors_origins: list[str] = ["http://localhost:4200"]

    @field_validator("cors_origins")
    @classmethod
    def validate_cors_origins(cls, origins: list[str]) -> list[str]:
        if not origins:
            raise ValueError("At least one explicit CORS origin must be configured.")

        normalized: list[str] = []
        for configured_origin in origins:
            origin = configured_origin.strip().rstrip("/")
            parsed = urlsplit(origin)
            try:
                port = parsed.port
            except ValueError as exc:
                raise ValueError("CORS origins must contain a valid port.") from exc

            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
                or "*" in origin
            ):
                raise ValueError(
                    "CORS origins must be explicit HTTP(S) origins without paths or wildcards."
                )

            scheme = parsed.scheme.lower()
            hostname = parsed.hostname.lower()
            if ":" in hostname:
                hostname = f"[{hostname}]"
            if port is not None and not (
                (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
            ):
                hostname = f"{hostname}:{port}"
            canonical_origin = f"{scheme}://{hostname}"
            if canonical_origin not in normalized:
                normalized.append(canonical_origin)

        return normalized

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
