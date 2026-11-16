from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="PQ_",
        extra="ignore",
        hide_input_in_errors=True,
    )

    service_name: str = Field(
        default="PrismQuant Political Monitoring API", min_length=1
    )
    environment: Literal["development", "test", "production"] = "development"
    searxng_url: str = ''

    @field_validator('searxng_url')
    @classmethod
    def local_search_origin(cls,value):
        from app.research_search import endpoint
        return endpoint(value)
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    cors_origins: list[str] = Field(default_factory=list)

    @field_validator("cors_origins")
    @classmethod
    def validate_cors_origins(cls, values: list[str]) -> list[str]:
        for value in values:
            origin = urlsplit(value)
            if (
                origin.scheme not in {"http", "https"}
                or not origin.hostname
                or origin.username is not None
                or origin.password is not None
                or origin.path or origin.query or origin.fragment
                or "*" in value or any(char.isspace() for char in value)
            ):
                raise ValueError("CORS origins must be exact HTTP(S) origins without paths")
            # Accessing port also rejects invalid or out-of-range ports.
            origin.port
        return list(dict.fromkeys(values))

    db_host: str = "127.0.0.1"
    db_port: int = Field(default=5432, ge=1, le=65535)
    db_name: str = "prismquant_political"
    db_user: str = "prismquant_app"
    db_password: SecretStr = SecretStr("")
    keyword_admin_token: SecretStr = SecretStr("")
    annotation_reviewer_tokens: SecretStr = SecretStr("")
    source_catalog_admin_token: SecretStr = SecretStr("")
    reminder_processing_enabled: bool = False
    reminder_poll_seconds: int = Field(default=10,ge=5,le=60)
    knowledge_indexing_enabled: bool = False
    knowledge_poll_seconds: int = Field(default=30,ge=10,le=300)
    fast_discovery_enabled: bool = False
    console_mode: Literal['research','operations'] = 'operations'
    runtime_instance: str = Field(default='',max_length=64)

    @field_validator('source_catalog_admin_token')
    @classmethod
    def catalog_admin(cls,value,info):
        token=value.get_secret_value()
        if token:
            if not 32<=len(token)<=256:raise ValueError('Catalog admin token needs 32..256 characters')
            if token==info.data.get('keyword_admin_token',SecretStr('')).get_secret_value():raise ValueError('Use an independent catalog credential')
            import json
            reviewers=info.data.get('annotation_reviewer_tokens',SecretStr('')).get_secret_value()
            if reviewers and token in json.loads(reviewers).values():raise ValueError('Use an independent catalog credential')
        return value

    @field_validator('annotation_reviewer_tokens')
    @classmethod
    def reviewer_credentials(cls,value,info):
        import json
        import re
        raw=value.get_secret_value()
        if not raw:return value
        try:
            if not raw.lstrip().startswith('{'):raise ValueError()
            pairs=json.loads(raw,object_pairs_hook=lambda values:values)
            if not isinstance(pairs,list) or not 1<=len(pairs)<=20:raise ValueError()
            credentials=dict(pairs)
            if len(credentials)!=len(pairs):raise ValueError()
            if any(not isinstance(k,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',k) or not isinstance(v,str) or not 32<=len(v)<=256 for k,v in credentials.items()):raise ValueError()
            if len(set(credentials.values()))!=len(credentials):raise ValueError()
            admin=info.data.get('keyword_admin_token',SecretStr('')).get_secret_value()
            if admin and admin in credentials.values():raise ValueError()
        except (ValueError,TypeError):raise ValueError('Reviewer credentials need distinct IDs and independent 32..256 character tokens') from None
        return value

    @field_validator('keyword_admin_token')
    @classmethod
    def admin_token_length(cls,value):
        if value.get_secret_value() and len(value.get_secret_value())<32:
            raise ValueError('Keyword admin token must contain at least 32 characters')
        return value
