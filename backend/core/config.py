from typing import List, Optional
from pydantic_settings import BaseSettings
from pydantic import field_validator

# reads from .env
class Settings(BaseSettings):
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    
    API_PREFIX: str = "/api"
    DEBUG: bool = False

    DATABASE_URL: str
    ALLOWED_ORIGINS: str = ""

    # LiteAPI (hotels). Use a sandbox key in .env during development.
    LITEAPI_API_KEY: Optional[str] = None
    LITEAPI_BASE_URL: str = "https://api.liteapi.travel/v3.0"
    LITEAPI_DAILY_LIMIT: int = 200

    # Claude API (the agent's LLM). Optional at startup; AI calls fail clearly without it.
    ANTHROPIC_API_KEY: Optional[str] = None
    LLM_MODEL: str = "claude-haiku-5-5"

    @field_validator("ALLOWED_ORIGINS")
    def parse_allowed_origins(cls, v: str) -> List[str]:
        return v.split(",") if v else []
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True

settings = Settings() #type: ignore[call-arg]
