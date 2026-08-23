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

    # Drops the user-row lock out of the GPU transaction. DEFAULT FALSE,
    # and the only thing that ever sets it true is Benchmark 2.
    #
    # It exists because DECISIONS.md says the broken version is built and
    # measured FIRST -- "reconstructing a broken build at Deadline 8 to
    # make a table look good is obvious and worthless" -- and because
    # Deadline 9 asks that a stranger reproduce our numbers from a fresh
    # clone. They cannot reproduce the BROKEN half of a broken-vs-fixed
    # table unless the broken build is still reachable. One documented
    # switch is the honest way to keep both columns reproducible; the
    # alternative is a table with one number nobody can re-derive.
    #
    # It removes a lock. It does not change any other logic, so the quota
    # arithmetic under it is the same arithmetic -- which is the point:
    # the bug Benchmark 2 shows is not bad arithmetic, it is correct
    # arithmetic on a value nothing was holding still.
    BENCHMARK_UNSAFE_NO_USER_LOCK: bool = False

    # The same idea for course registration, and the same justification.
    # Drops `FOR UPDATE` from the offering read while leaving everything
    # else -- including `populate_existing()` -- exactly as it is, so the
    # broken build reads a FRESH value and still loses updates. That is
    # the honest broken build: not a stale read, but a correct read that
    # nothing was holding still. Benchmark 1 is the only thing that sets
    # it, and it defaults to false.
    BENCHMARK_UNSAFE_NO_OFFERING_LOCK: bool = False

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()