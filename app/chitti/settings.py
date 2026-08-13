from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://chitti:chitti@localhost:5432/chitti"
    litellm_base_url: str = "http://localhost:4000"
    litellm_master_key: str = ""
    chitti_provider: str = "litellm"
    chitti_username: str = "akirah"
    chitti_password_hash: str = ""
    chitti_session_ttl_minutes: int = 480
    chitti_auth_state_path: str = "/app/data/auth_state.json"
    chitti_trusted_proxy_ip: str = "172.31.250.2"
    telegram_bot_token: str = ""
    allowed_telegram_user_ids: str = ""
    profile_path: str = "/app/profile/PROFILE.md"
    project_root: str = "/app/projects"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    display_timezone: str = "Asia/Dubai"
    preview_root: str = "/app/previews"
    preview_staging_root: str = "/app/preview-staging"
    preview_ttl_hours: int = 72
    preview_max_bytes: int = 200 * 1024 * 1024
    preview_max_count: int = 4
    google_client_id: str = ""
    google_client_secret: str = ""
    google_oauth_redirect_uri: str = "https://chitti.local/google/callback"
    google_credentials_key: str = ""
    google_sync_interval_seconds: int = 300
    google_recent_mail_days: int = 30
    google_initial_mail_limit: int = 100
    google_calendar_window_days: int = 30

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def allowed_ids(self) -> set[int]:
        return {
            int(item.strip()) for item in self.allowed_telegram_user_ids.split(",") if item.strip()
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
