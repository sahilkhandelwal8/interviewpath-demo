from __future__ import annotations

import asyncio
import os
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Literal

import httpx

from .models import CallRecord, Query, RetrievedPage, SearchResult


ProviderName = Literal["firecrawl", "exa"]


def _error_text(response: httpx.Response) -> str:
    text = response.text.replace("\n", " ").strip()
    return text[:300] or response.reason_phrase


def _usage(payload: dict[str, Any]) -> dict[str, Any]:
    usage: dict[str, Any] = {}
    for key in ("costDollars", "creditsUsed", "usage"):
        if key in payload:
            usage[key] = payload[key]
    return usage


class Provider(ABC):
    name: ProviderName

    def __init__(self, api_key: str, base_url: str, timeout_seconds: int) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True)
        self.calls: list[CallRecord] = []

    async def close(self) -> None:
        await self.client.aclose()

    @abstractmethod
    async def search(self, query: Query, limit: int) -> list[SearchResult]: ...

    @abstractmethod
    async def retrieve(self, result: SearchResult, page_id: str, max_chars: int) -> RetrievedPage: ...

    def _record(
        self,
        *,
        operation: Literal["search", "retrieve"],
        label: str,
        started_at: datetime,
        started_clock: float,
        success: bool,
        response: httpx.Response | None = None,
        error: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        self.calls.append(
            CallRecord(
                operation=operation,
                request_label=label,
                started_at=started_at,
                duration_ms=round((perf_counter() - started_clock) * 1000),
                success=success,
                status_code=response.status_code if response is not None else None,
                error=error,
                request_id=(
                    response.headers.get("x-request-id") or response.headers.get("request-id")
                    if response is not None
                    else None
                ),
                usage=usage or {},
            )
        )


class FirecrawlProvider(Provider):
    name: ProviderName = "firecrawl"

    async def search(self, query: Query, limit: int) -> list[SearchResult]:
        started_at, started_clock = datetime.now(UTC), perf_counter()
        response: httpx.Response | None = None
        try:
            response = await self.client.post(
                f"{self.base_url}/search",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"query": query.text, "limit": limit, "highlights": True},
            )
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data", {})
            items = data if isinstance(data, list) else data.get("web", [])
            results = [
                SearchResult(
                    area=query.area,
                    query_id=query.id,
                    rank=index,
                    url=item["url"],
                    title=item.get("title"),
                    snippet=item.get("description") or item.get("snippet"),
                    published_at=item.get("publishedDate") or item.get("date"),
                )
                for index, item in enumerate(items[:limit], start=1)
                if item.get("url")
            ]
            self._record(
                operation="search",
                label=query.id,
                started_at=started_at,
                started_clock=started_clock,
                success=True,
                response=response,
                usage=_usage(payload),
            )
            return results
        except Exception as exc:  # request failures are part of the comparison artifact
            self._record(
                operation="search",
                label=query.id,
                started_at=started_at,
                started_clock=started_clock,
                success=False,
                response=response,
                error=_error_text(response) if response is not None else str(exc),
            )
            return []

    async def retrieve(self, result: SearchResult, page_id: str, max_chars: int) -> RetrievedPage:
        started_at, started_clock = datetime.now(UTC), perf_counter()
        response: httpx.Response | None = None
        try:
            response = await self.client.post(
                f"{self.base_url}/scrape",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"url": result.url, "formats": ["markdown"], "onlyMainContent": True},
            )
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data", {})
            content = (data.get("markdown") or data.get("content") or "")[:max_chars]
            metadata = data.get("metadata") or {}
            usable = bool(content.strip())
            self._record(
                operation="retrieve",
                label=page_id,
                started_at=started_at,
                started_clock=started_clock,
                success=usable,
                response=response,
                error=None if usable else "empty content",
                usage=_usage(payload),
            )
            return RetrievedPage(
                page_id=page_id,
                url=result.url,
                title=metadata.get("title") or result.title,
                areas=[result.area],
                content=content,
                published_at=metadata.get("publishedTime") or result.published_at,
                retrieved_at=datetime.now(UTC),
                usable=usable,
                error=None if usable else "empty content",
            )
        except Exception as exc:
            error = _error_text(response) if response is not None else str(exc)
            self._record(
                operation="retrieve",
                label=page_id,
                started_at=started_at,
                started_clock=started_clock,
                success=False,
                response=response,
                error=error,
            )
            return RetrievedPage(
                page_id=page_id,
                url=result.url,
                title=result.title,
                areas=[result.area],
                retrieved_at=datetime.now(UTC),
                usable=False,
                error=error,
            )


class ExaProvider(Provider):
    name: ProviderName = "exa"

    async def search(self, query: Query, limit: int) -> list[SearchResult]:
        started_at, started_clock = datetime.now(UTC), perf_counter()
        response: httpx.Response | None = None
        try:
            response = await self.client.post(
                f"{self.base_url}/search",
                headers={"x-api-key": self.api_key},
                json={
                    "query": query.text,
                    "type": "auto",
                    "numResults": limit,
                    "contents": {"highlights": True},
                },
            )
            response.raise_for_status()
            payload = response.json()
            results = []
            for index, item in enumerate(payload.get("results", [])[:limit], start=1):
                if not item.get("url"):
                    continue
                highlights = item.get("highlights") or []
                passage = (
                    highlights.strip()
                    if isinstance(highlights, str)
                    else "\n\n".join(str(value).strip() for value in highlights if str(value).strip())
                )
                results.append(
                    SearchResult(
                        area=query.area,
                        query_id=query.id,
                        rank=index,
                        url=item["url"],
                        title=item.get("title"),
                        snippet=passage or item.get("text") or item.get("summary"),
                        published_at=item.get("publishedDate"),
                    )
                )
            self._record(
                operation="search",
                label=query.id,
                started_at=started_at,
                started_clock=started_clock,
                success=True,
                response=response,
                usage=_usage(payload),
            )
            return results
        except Exception as exc:
            self._record(
                operation="search",
                label=query.id,
                started_at=started_at,
                started_clock=started_clock,
                success=False,
                response=response,
                error=_error_text(response) if response is not None else str(exc),
            )
            return []

    async def retrieve(self, result: SearchResult, page_id: str, max_chars: int) -> RetrievedPage:
        started_at, started_clock = datetime.now(UTC), perf_counter()
        response: httpx.Response | None = None
        try:
            response = await self.client.post(
                f"{self.base_url}/contents",
                headers={"x-api-key": self.api_key},
                json={"ids": [result.url], "text": True, "maxAgeHours": 0},
            )
            response.raise_for_status()
            payload = response.json()
            item = (payload.get("results") or [{}])[0]
            content = (item.get("text") or "")[:max_chars]
            usable = bool(content.strip())
            self._record(
                operation="retrieve",
                label=page_id,
                started_at=started_at,
                started_clock=started_clock,
                success=usable,
                response=response,
                error=None if usable else "empty content",
                usage=_usage(payload),
            )
            return RetrievedPage(
                page_id=page_id,
                url=result.url,
                title=item.get("title") or result.title,
                areas=[result.area],
                content=content,
                published_at=item.get("publishedDate") or result.published_at,
                retrieved_at=datetime.now(UTC),
                usable=usable,
                error=None if usable else "empty content",
            )
        except Exception as exc:
            error = _error_text(response) if response is not None else str(exc)
            self._record(
                operation="retrieve",
                label=page_id,
                started_at=started_at,
                started_clock=started_clock,
                success=False,
                response=response,
                error=error,
            )
            return RetrievedPage(
                page_id=page_id,
                url=result.url,
                title=result.title,
                areas=[result.area],
                retrieved_at=datetime.now(UTC),
                usable=False,
                error=error,
            )


def create_provider(name: ProviderName, *, base_url: str, timeout_seconds: int) -> Provider:
    key_name = "FIRECRAWL_API_KEY" if name == "firecrawl" else "EXA_API_KEY"
    api_key = os.environ.get(key_name)
    if not api_key:
        raise RuntimeError(f"Missing {key_name}. Add it to .env or the process environment.")
    cls = FirecrawlProvider if name == "firecrawl" else ExaProvider
    return cls(api_key=api_key, base_url=base_url, timeout_seconds=timeout_seconds)


async def search_all(provider: Provider, queries: list[Query], limit: int, on_result=None) -> list[SearchResult]:
    async def search(query):
        results = await provider.search(query, limit)
        if on_result is not None:
            on_result(query, results, [c for c in provider.calls if c.request_label == query.id])
        return results
    groups = await asyncio.gather(*(search(query) for query in queries))
    return [item for group in groups for item in group]
