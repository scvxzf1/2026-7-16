from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


ProxyMode = Literal["direct", "prefer", "required"]


class SitePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_concurrency: int = Field(default=2, ge=1, le=128)
    retry_limit: int = Field(default=2, ge=0, le=20)
    backoff_base_seconds: float = Field(default=2.0, ge=0.0, le=3600.0)
    proxy_mode: ProxyMode = "prefer"
    probe_url: str | None = None
    probe_before_use: bool = False
    node_tags: list[str] = Field(default_factory=list, max_length=32)
    http_timeout: float = Field(default=30.0, ge=1.0, le=3600.0)
    gallery_retries: int = Field(default=2, ge=0, le=50)
    task_timeout_seconds: float = Field(default=0.0, ge=0.0, le=604800.0)
    extra_args: list[str] = Field(default_factory=list, max_length=128)

    @field_validator("probe_url")
    @classmethod
    def validate_probe_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        text = value.strip()
        if not text.lower().startswith("https://"):
            raise ValueError("probe_url 必须使用 https://")
        return text

    @field_validator("node_tags")
    @classmethod
    def normalize_tags(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            tag = str(value).strip().lower()
            if tag and tag not in seen:
                result.append(tag)
                seen.add(tag)
        return result

    @field_validator("extra_args")
    @classmethod
    def stringify_args(cls, values: list[str]) -> list[str]:
        return [str(value) for value in values]


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=4, max_length=8192)
    site: str | None = Field(default=None, min_length=1, max_length=128)
    output_dir: str | None = Field(default=None, max_length=2048)
    proxy_mode: ProxyMode | None = None
    max_attempts: int | None = Field(default=None, ge=1, le=20)
    priority: int = Field(default=0, ge=-1000, le=1000)
    credentials_ref: str | None = Field(default=None, max_length=128)
    cookies_file: str | None = Field(default=None, max_length=2048)
    config_file: str | None = Field(default=None, max_length=2048)
    extra_args: list[str] = Field(default_factory=list, max_length=128)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        text = value.strip()
        lower = text.lower()
        if lower.startswith(("http://", "https://")):
            return text
        if re.match(r"^[a-z0-9_-]+:(?:r:)?https?://", lower):
            return text
        raise ValueError("url 必须是 HTTP(S) 图站地址或 gallery-dl 提取器前缀地址")

    @field_validator("site")
    @classmethod
    def validate_site(cls, value: str | None) -> str | None:
        if value is None:
            return None
        site = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._:-]{0,127}", site):
            raise ValueError("site 格式无效")
        return site

    @field_validator("credentials_ref")
    @classmethod
    def validate_credentials_ref(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        ref = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", ref):
            raise ValueError("credentials_ref 格式无效")
        return ref

    @field_validator("extra_args")
    @classmethod
    def stringify_args(cls, values: list[str]) -> list[str]:
        return [str(value) for value in values]


class RetryRequest(BaseModel):
    additional_attempts: int = Field(default=1, ge=1, le=20)


class ProxyStartRequest(BaseModel):
    force_refresh: bool = True
    probe_url: str | None = None

    @field_validator("probe_url")
    @classmethod
    def validate_probe_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        text = value.strip()
        if not text.lower().startswith("https://"):
            raise ValueError("probe_url 必须使用 https://")
        return text


class ProxyProbeRequest(BaseModel):
    site: str | None = None
    target_url: str | None = None
    node_id: str | None = None

    @field_validator("target_url")
    @classmethod
    def validate_target(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        text = value.strip()
        if not text.lower().startswith("https://"):
            raise ValueError("target_url 必须使用 https://")
        return text


class ProxyStopRequest(BaseModel):
    force: bool = False
