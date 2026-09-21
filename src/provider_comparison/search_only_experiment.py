from __future__ import annotations

import asyncio
import os
import re
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import Settings
from .models import Prospect, ResearchArea


YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
NUMBER_RE = re.compile(r"(?<!\w)(?:[$€£]\s*)?\d[\d,.]*(?:\s*(?:%|million|billion|employees?|jobs?|roles?))?", re.I)


def build_targeted_queries(prospect: Prospect) -> list[dict[str, str]]:
    name = f'"{prospect.name}"'
    company = f'"{prospect.company}"'
    return [
        {
            "id": f"{prospect.id}-profile",
            "area": ResearchArea.PROFESSIONAL_PROFILE.value,
            "query": f"{name} {company} current role biography interview podcast",
            "need": "Current role and attributable professional background",
        },
        {
            "id": f"{prospect.id}-developments",
            "area": ResearchArea.COMPANY_DEVELOPMENTS.value,
            "query": f"{company} funding expansion acquisition layoffs leadership 2025 2026",
            "need": "Dated company developments that could affect recruiting",
        },
        {
            "id": f"{prospect.id}-recruiting",
            "area": ResearchArea.RECRUITING_CONTEXT.value,
            "query": f"{name} {company} recruiting hiring talent acquisition interview",
            "need": "Attributable statements or facts about recruiting context",
        },
        {
            "id": f"{prospect.id}-footprint",
            "area": ResearchArea.HIRING_FOOTPRINT.value,
            "query": f"{company} careers open jobs locations hiring",
            "need": "Current public hiring footprint and careers source",
        },
    ]


def build_evidence_queries(prospect: Prospect) -> list[dict[str, str]]:
    """Question-style queries that explicitly request evidence metadata.

    These test whether query wording can make provider search highlights sufficiently
    complete for direct extraction. Hiring statistics are intentionally excluded.
    """
    return [
        {
            "id": f"{prospect.id}-profile-evidence",
            "area": ResearchArea.PROFESSIONAL_PROFILE.value,
            "query": (
                f"What is {prospect.name}'s current role and relevant recruiting background "
                f"at {prospect.company}? Include source attribution and publication or event date."
            ),
            "need": "Current role and attributable professional background, with a date when available",
        },
        {
            "id": f"{prospect.id}-developments-evidence",
            "area": ResearchArea.COMPANY_DEVELOPMENTS.value,
            "query": (
                f"What company developments at {prospect.company} since 2025 materially affect "
                "recruiting or hiring? Include the exact event date and source."
            ),
            "need": "Dated company development that materially affects recruiting",
        },
        {
            "id": f"{prospect.id}-recruiting-evidence",
            "area": ResearchArea.RECRUITING_CONTEXT.value,
            "query": (
                f"What have {prospect.name} or {prospect.company} publicly stated about recruiting "
                "priorities, hiring processes, or recruiting tools? Include speaker, source, and date."
            ),
            "need": "Attributable recruiting context with source and date",
        },
    ]


def _items(payload: dict[str, Any], provider: str) -> list[dict[str, Any]]:
    if provider == "firecrawl":
        data = payload.get("data") or {}
        return data if isinstance(data, list) else data.get("web") or []
    return payload.get("results") or []


def _passage(item: dict[str, Any], provider: str) -> str:
    if provider == "firecrawl":
        return (item.get("description") or item.get("snippet") or "").strip()
    highlights = item.get("highlights") or []
    if isinstance(highlights, str):
        return highlights.strip()
    return "\n\n".join(str(value).strip() for value in highlights if str(value).strip())


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def normalize_result(
    *, provider: str, prospect: Prospect, query: dict[str, str], rank: int, item: dict[str, Any]
) -> dict[str, Any]:
    passage = _passage(item, provider)
    lowered = passage.casefold()
    person_named = prospect.name.casefold() in lowered
    company_named = prospect.company.casefold() in lowered
    published_at = item.get("publishedDate") or item.get("date")
    return {
        "rank": rank,
        "title": item.get("title"),
        "url": item.get("url"),
        "domain": _domain(item.get("url") or ""),
        "published_at": published_at,
        "passage": passage,
        "indicators": {
            "has_passage": bool(passage),
            "passage_chars": len(passage),
            "person_named": person_named,
            "company_named": company_named,
            "target_named": person_named or company_named,
            "date_present": bool(published_at or YEAR_RE.search(passage)),
            "numeric_detail_present": bool(NUMBER_RE.search(passage)),
            "potentially_usable": bool(len(passage) >= 100 and (person_named or company_named)),
        },
    }


async def _search(
    client: httpx.AsyncClient,
    *,
    provider: str,
    api_key: str,
    base_url: str,
    prospect: Prospect,
    query: dict[str, str],
    limit: int,
) -> dict[str, Any]:
    started = datetime.now(UTC)
    clock = perf_counter()
    if provider == "firecrawl":
        url = f"{base_url.rstrip('/')}/search"
        headers = {"Authorization": f"Bearer {api_key}"}
        body: dict[str, Any] = {
            "query": query["query"],
            "limit": limit,
            "sources": ["web"],
            "highlights": True,
        }
    else:
        url = f"{base_url.rstrip('/')}/search"
        headers = {"x-api-key": api_key}
        body = {
            "query": query["query"],
            "type": "auto",
            "numResults": limit,
            "contents": {"highlights": True},
        }
    response: httpx.Response | None = None
    try:
        response = await client.post(url, headers=headers, json=body)
        response.raise_for_status()
        payload = response.json()
        results = [
            normalize_result(
                provider=provider,
                prospect=prospect,
                query=query,
                rank=index,
                item=item,
            )
            for index, item in enumerate(_items(payload, provider)[:limit], start=1)
            if item.get("url")
        ]
        return {
            **query,
            "status": "success",
            "started_at": started.isoformat(),
            "duration_ms": round((perf_counter() - clock) * 1000),
            "request_id": response.headers.get("x-request-id") or payload.get("requestId"),
            "cost": payload.get("costDollars") or payload.get("creditsUsed"),
            "results": results,
        }
    except Exception as exc:
        error = str(exc)
        if response is not None:
            error = f"HTTP {response.status_code}: {response.text[:300].replace(chr(10), ' ')}"
        return {
            **query,
            "status": "failed",
            "started_at": started.isoformat(),
            "duration_ms": round((perf_counter() - clock) * 1000),
            "request_id": response.headers.get("x-request-id") if response is not None else None,
            "error": error,
            "results": [],
        }


def summarize(provider_runs: list[dict[str, Any]]) -> dict[str, Any]:
    results = [result for run in provider_runs for result in run["results"]]
    with_passage = [result for result in results if result["indicators"]["has_passage"]]
    usable = [result for result in results if result["indicators"]["potentially_usable"]]
    dated = [result for result in results if result["indicators"]["date_present"]]
    return {
        "queries": len(provider_runs),
        "successful_queries": sum(run["status"] == "success" for run in provider_runs),
        "results": len(results),
        "results_with_passage": len(with_passage),
        "potentially_usable_results": len(usable),
        "dated_results": len(dated),
        "total_passage_chars": sum(result["indicators"]["passage_chars"] for result in results),
        "median_passage_chars": _median(
            [result["indicators"]["passage_chars"] for result in with_passage]
        ),
    }


def _median(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return round((ordered[middle - 1] + ordered[middle]) / 2)


async def run_search_only_experiment(
    *, prospects: list[Prospect], settings: Settings, limit: int = 5, query_style: str = "baseline"
) -> dict[str, Any]:
    keys = {
        "firecrawl": os.environ["FIRECRAWL_API_KEY"],
        "exa": os.environ["EXA_API_KEY"],
    }
    bases = {
        "firecrawl": settings.firecrawl_base_url,
        "exa": settings.exa_base_url,
    }
    timeout = httpx.Timeout(settings.request_timeout_seconds)
    query_builder = build_evidence_queries if query_style == "evidence" else build_targeted_queries
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        calls = []
        for prospect in prospects:
            for query in query_builder(prospect):
                for provider in ("firecrawl", "exa"):
                    calls.append(
                        _search(
                            client,
                            provider=provider,
                            api_key=keys[provider],
                            base_url=bases[provider],
                            prospect=prospect,
                            query=query,
                            limit=limit,
                        )
                    )
        completed = await asyncio.gather(*calls)

    records: list[dict[str, Any]] = []
    index = 0
    for prospect in prospects:
        provider_runs = {"firecrawl": [], "exa": []}
        for _query in query_builder(prospect):
            for provider in ("firecrawl", "exa"):
                provider_runs[provider].append(completed[index])
                index += 1
        records.append(
            {
                "prospect": prospect.model_dump(mode="json"),
                "providers": provider_runs,
            }
        )

    totals: dict[str, list[dict[str, Any]]] = {"firecrawl": [], "exa": []}
    for record in records:
        for provider in totals:
            totals[provider].extend(record["providers"][provider])
    return {
        "schema_version": "1.0",
        "experiment": "search_highlights_only",
        "query_style": query_style,
        "created_at": datetime.now(UTC).isoformat(),
        "rules": {
            "separate_page_fetches": False,
            "llm_extraction": False,
            "results_per_query": limit,
            "manual_review_required": True,
            "potentially_usable_heuristic": "passage >= 100 characters and names the person or company",
        },
        "summary": {provider: summarize(runs) for provider, runs in totals.items()},
        "prospects": records,
    }


def render_markdown(artifact: dict[str, Any], max_results_per_query: int = 3) -> str:
    lines = [
        "# Search-only evidence experiment",
        "",
        f"Generated: {artifact['created_at']}",
        "",
        "No separate pages were fetched and no LLM processed the results. “Potentially usable” is only a mechanical triage flag; the passages below still require human evidence review.",
        "",
        "## Aggregate",
        "",
        "| Provider | Successful queries | Results | With passage | Potentially usable | Dated | Median chars |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for provider, summary in artifact["summary"].items():
        lines.append(
            f"| {provider.title()} | {summary['successful_queries']}/{summary['queries']} | "
            f"{summary['results']} | {summary['results_with_passage']} | "
            f"{summary['potentially_usable_results']} | {summary['dated_results']} | "
            f"{summary['median_passage_chars']} |"
        )
    for record in artifact["prospects"]:
        prospect = record["prospect"]
        lines.extend(["", f"## {prospect['id']} — {prospect['name']} / {prospect['company']}"])
        for provider, queries in record["providers"].items():
            lines.extend(["", f"### {provider.title()}"])
            for query in queries:
                lines.extend(
                    [
                        "",
                        f"#### {query['area']}",
                        "",
                        f"Query: `{query['query']}`",
                        "",
                        f"Need: {query['need']}",
                        "",
                    ]
                )
                if query["status"] != "success":
                    lines.append(f"Request failed: {query.get('error', 'unknown error')}")
                    continue
                for result in query["results"][:max_results_per_query]:
                    flags = result["indicators"]
                    lines.extend(
                        [
                            f"{result['rank']}. [{result['title'] or result['domain']}]({result['url']})",
                            f"   - Date: {result['published_at'] or 'not returned'}; passage chars: {flags['passage_chars']}; target named: {flags['target_named']}; potentially usable: {flags['potentially_usable']}",
                            f"   - Passage: {result['passage'].replace(chr(10), ' ')[:1200] or '[none]'}",
                            "",
                        ]
                    )
    return "\n".join(lines).rstrip() + "\n"
