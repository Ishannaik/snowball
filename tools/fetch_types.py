#!/usr/bin/env python3
"""Shared retrieval records. Secret fields do not belong on these types."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AccessRequest:
    acquisition_id: str
    page_id: int
    url: str
    private: bool
    allowed_modes: tuple[str, ...]
    approved_resource_origins: tuple[str, ...]
    allowed_path_prefixes: tuple[str, ...]
    session_id: str
    deadline_at: float


@dataclass
class FetchResult:
    canon: str
    route: str
    outcome: str  # ok | walled | outage | failure | ssrf_blocked | oversize | deadline
    status: int | None = None
    title: str = ""
    text: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)
    wall_kind: str | None = None
    retry_after: int | None = None
    pinned_ip: str | None = None
    ms: int = 0
    nbytes: int = 0
    final_url: str | None = None
    truncated: bool = False


@dataclass
class SearchHit:
    url: str
    title: str = ""
    text: str = ""
    route: str = "direct"
