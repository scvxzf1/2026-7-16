from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ProxyMode = Literal["direct", "prefer", "required"]
class PixivOAuthCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=16, max_length=128, pattern=r"^[a-fA-F0-9]+$")
    callback: str = Field(min_length=1, max_length=8192)


class SitePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_concurrency: int = Field(default=20, ge=1, le=128)
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


class SearchSourceOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proxy_mode: ProxyMode | None = None
    credentials_ref: str | None = Field(default=None, max_length=128)
    cookies_file: str | None = Field(default=None, max_length=2048)
    config_file: str | None = Field(default=None, max_length=2048)
    search_extra_args: list[str] = Field(default_factory=list, max_length=128)
    timeout_seconds: float = Field(default=180.0, ge=5.0, le=3600.0)

    @field_validator("credentials_ref")
    @classmethod
    def validate_credentials_ref(cls, value: str | None) -> str | None:
        return TaskCreate.validate_credentials_ref(value)

    @field_validator("search_extra_args")
    @classmethod
    def stringify_args(cls, values: list[str]) -> list[str]:
        return [str(value) for value in values]


class SearchRequest(SearchSourceOptions):
    model_config = ConfigDict(extra="forbid")

    keyword: str = Field(min_length=1, max_length=1000)
    sites: list[str] = Field(
        default_factory=lambda: ["danbooru", "twitter", "pixiv", "exhentai"],
        min_length=1,
        max_length=4,
    )
    limit: int = Field(default=20, ge=1, le=200)
    source_options: dict[str, SearchSourceOptions] = Field(default_factory=dict, max_length=8)

    @field_validator("keyword")
    @classmethod
    def normalize_keyword(cls, value: str) -> str:
        keyword = value.strip()
        if not keyword:
            raise ValueError("keyword 为空")
        return keyword

    @field_validator("sites")
    @classmethod
    def normalize_sites(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            site = str(value).strip().lower()
            if not re.fullmatch(r"[a-z0-9][a-z0-9._:-]{0,127}", site):
                raise ValueError("sites 中包含格式无效的站点名")
            if site not in seen:
                result.append(site)
                seen.add(site)
        return result


class CrawlAddress(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str = Field(min_length=4, max_length=8192)
    id: str | None = Field(default=None, max_length=500)
    label: str | None = Field(default=None, max_length=500)
    address_type: str | None = Field(default=None, max_length=100)
    extra_args: list[str] = Field(default_factory=list, max_length=128)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return TaskCreate.validate_url(value)

    @field_validator("extra_args")
    @classmethod
    def stringify_args(cls, values: list[str]) -> list[str]:
        return [str(value) for value in values]


class CrawlSource(BaseModel):
    # A complete search source object can be posted back after its addresses are selected.
    model_config = ConfigDict(extra="ignore")

    site: str = Field(min_length=1, max_length=128)
    addresses: list[CrawlAddress] = Field(min_length=1, max_length=500)
    proxy_mode: ProxyMode | None = None
    max_attempts: int | None = Field(default=None, ge=1, le=20)
    priority: int = Field(default=0, ge=-1000, le=1000)
    credentials_ref: str | None = Field(default=None, max_length=128)
    cookies_file: str | None = Field(default=None, max_length=2048)
    config_file: str | None = Field(default=None, max_length=2048)
    extra_args: list[str] = Field(default_factory=list, max_length=128)
    discovery_extra_args: list[str] = Field(default_factory=list, max_length=128)
    timeout_seconds: float = Field(default=180.0, ge=5.0, le=3600.0)

    @field_validator("site")
    @classmethod
    def validate_site(cls, value: str) -> str:
        return TaskCreate.validate_site(value)

    @field_validator("credentials_ref")
    @classmethod
    def validate_credentials_ref(cls, value: str | None) -> str | None:
        return TaskCreate.validate_credentials_ref(value)

    @field_validator("extra_args", "discovery_extra_args")
    @classmethod
    def stringify_args(cls, values: list[str]) -> list[str]:
        return [str(value) for value in values]


class CrawlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: list[CrawlSource] = Field(min_length=1, max_length=20)
    concurrency: int = Field(default=20, ge=1, le=128)
    max_tasks: int = Field(default=10000, ge=1, le=100000)
    output_dir: str | None = Field(default=None, max_length=2048)
    proxy_mode: ProxyMode | None = None
    max_attempts: int | None = Field(default=None, ge=1, le=20)
    priority: int = Field(default=0, ge=-1000, le=1000)
    credentials_ref: str | None = Field(default=None, max_length=128)
    cookies_file: str | None = Field(default=None, max_length=2048)
    config_file: str | None = Field(default=None, max_length=2048)
    extra_args: list[str] = Field(default_factory=list, max_length=128)
    discovery_extra_args: list[str] = Field(default_factory=list, max_length=128)
    timeout_seconds: float = Field(default=180.0, ge=5.0, le=3600.0)

    @field_validator("credentials_ref")
    @classmethod
    def validate_credentials_ref(cls, value: str | None) -> str | None:
        return TaskCreate.validate_credentials_ref(value)

    @field_validator("extra_args", "discovery_extra_args")
    @classmethod
    def stringify_args(cls, values: list[str]) -> list[str]:
        return [str(value) for value in values]

    @model_validator(mode="after")
    def unique_sources(self):
        names = [source.site for source in self.sources]
        if len(names) != len(set(names)):
            raise ValueError("sources 中同一来源只保留一个分组")
        return self


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
