import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    upstream_base_url: str = "https://api.deepseek.com"
    upstream_api_key: str = ""
    upstream_model: str = "deepseek-v4-flash"

    max_decomposition_depth: int = 3
    max_tool_rounds_per_phase: int = 25

    proxy_host: str = "127.0.0.1"
    proxy_port: int = 8000

    request_timeout_seconds: float = 120.0

    session_ttl_seconds: float = 1800.0
    max_sessions: int = 200

    expose_reasoning_content: bool = True

    def resolved_api_key(self) -> str:
        return self.upstream_api_key or os.environ.get("DEEPSEEK_API_KEY", "")


settings = Settings()
