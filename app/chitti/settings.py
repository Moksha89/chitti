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
    telegram_bot_token: str = ""
    allowed_telegram_user_ids: str = ""
    profile_path: str = "/app/profile/PROFILE.md"
    project_root: str = "/app/projects"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def allowed_ids(self) -> set[int]:
        return {
            int(item.strip()) for item in self.allowed_telegram_user_ids.split(",") if item.strip()
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
