from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/spoilage"
    test_database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5433/spoilage_test"
    jwt_secret: str = "change-me-long-random"
    jwt_exp_minutes: int = 60
    cron_secret: str = "change-me"
    allowed_origins: str = "http://localhost:8000"
    demo_mode: bool = True
    demo_password: str = "demo1234"
    supabase_url: str = ""
    supabase_anon_key: str = ""

    @property
    def origins(self):
        origins = [x.strip() for x in self.allowed_origins.split(",") if x.strip()]
        if not self.demo_mode and "*" in origins:
            raise ValueError("Production CORS requires explicit origins")
        return origins


@lru_cache
def get_settings():
    return Settings()


settings = get_settings()
