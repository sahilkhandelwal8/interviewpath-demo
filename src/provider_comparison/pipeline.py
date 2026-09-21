from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Protocol

from .config import Settings, write_json
from .models import CallRecord, EvidenceBundle, Prospect, Query, RetrievedPage, RunArtifact
from .providers import Provider, ProviderName, create_provider, search_all
from .selection import select_results


class Extractor(Protocol):
    async def extract(self, prospect: Prospect, pages: list) -> tuple[EvidenceBundle, CallRecord]: ...


def _careers_candidates(urls: list[str]) -> list[str]:
    tokens = ("career", "jobs.", "job-boards", "ashbyhq", "greenhouse", "lever.co")
    return [url for url in urls if any(token in url.lower() for token in tokens)]


async def run_prospect(
    *,
    run_id: str,
    repetition: int,
    provider_name: ProviderName,
    prospect: Prospect,
    queries: list[Query],
    settings: Settings,
    extractor: Extractor,
    on_result=None,
) -> RunArtifact:
    started_at, started_clock = datetime.now(UTC), perf_counter()
    provider = create_provider(
        provider_name,
        base_url=settings.firecrawl_base_url if provider_name == "firecrawl" else settings.exa_base_url,
        timeout_seconds=settings.request_timeout_seconds,
    )
    search_results = []
    selected = []
    pages = []
    evidence = EvidenceBundle()
    extraction_record: CallRecord | None = None
    status = "completed"
    provider_duration_ms = 0
    extraction_duration_ms = 0

    async def execute() -> None:
        nonlocal search_results, selected, pages, evidence, extraction_record
        nonlocal provider_duration_ms, extraction_duration_ms, status
        provider_clock = perf_counter()
        search_results = await search_all(provider, queries, settings.results_per_search, on_result)
        selected = select_results(search_results, settings.max_pages)
        retrieved_at = datetime.now(UTC)
        pages = [
            RetrievedPage(
                page_id=f"RESULT-{index:02d}",
                url=item.url,
                title=item.title,
                areas=[item.area],
                content=(item.snippet or "")[: settings.max_chars_per_page],
                published_at=item.published_at,
                retrieved_at=retrieved_at,
                usable=bool((item.snippet or "").strip()),
                error=None if (item.snippet or "").strip() else "search returned no passage",
            )
            for index, item in enumerate(selected, start=1)
        ]
        provider_duration_ms = round((perf_counter() - provider_clock) * 1000)
        evidence, extraction_record = await extractor.extract(prospect, pages)
        extraction_duration_ms = extraction_record.duration_ms
        if any(not call.success for call in provider.calls) or not extraction_record.success:
            status = "partial"

    try:
        await execute()
    except Exception as exc:
        status = "failed"
        extraction_record = CallRecord(
            operation="extract",
            request_label=f"{prospect.id}-pipeline",
            started_at=datetime.now(UTC),
            duration_ms=0,
            success=False,
            error=str(exc)[:500],
        )
    finally:
        await provider.close()

    completed_at = datetime.now(UTC)
    calls = list(provider.calls)
    if extraction_record is not None:
        calls.append(extraction_record)
    selected_urls = [item.url for item in selected]
    return RunArtifact(
        run_id=run_id,
        repetition=repetition,
        provider=provider_name,
        prospect=prospect,
        started_at=started_at,
        completed_at=completed_at,
        status=status,
        queries=queries,
        search_results=search_results,
        selected_urls=selected_urls,
        pages=pages,
        evidence=evidence,
        calls=calls,
        total_duration_ms=round((perf_counter() - started_clock) * 1000),
        provider_duration_ms=provider_duration_ms,
        extraction_duration_ms=extraction_duration_ms,
        shared_enrichment={
            "status": "not_run",
            "careers_candidates": _careers_candidates(selected_urls),
            "note": "ATS enrichment is a shared hook and is excluded from provider scoring. V1 uses search passages without separate page retrieval.",
        },
    )


def artifact_path(root: Path, artifact: RunArtifact) -> Path:
    return root / artifact.provider / artifact.prospect.id / f"run-{artifact.repetition:02d}.json"


def save_artifact(root: Path, artifact: RunArtifact) -> Path:
    path = artifact_path(root, artifact)
    write_json(path, artifact)
    return path
