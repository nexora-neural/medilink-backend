from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "MediLink API"
    app_env: str = "local"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    firebase_project_id: str | None = None
    private_upload_dir: str = "./data/private_uploads"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
