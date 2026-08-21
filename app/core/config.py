from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Every API route is mounted under this; health checks deliberately are
# not (they are infrastructure, and docker-compose should not have to
# track an API version). It lives here rather than in main.py because
# core/dependencies.py needs it for the OAuth2 tokenUrl -- and importing
# main.py from a dependency is a cycle, since main.py imports the routers
# that import the dependency. Settled in session 6; see DECISIONS.md.
API_PREFIX = "/api/v1"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    POSTGRES_USER: str
    POSTGRES_PASSWORD: str
    POSTGRES_DB: str
    POSTGRES_HOST: str = "db"
    POSTGRES_PORT: int = 5432

    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()