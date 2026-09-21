from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from time import perf_counter

from google import genai

from .models import CallRecord, EvidenceBundle, Prospect, RetrievedPage


EXTRACTION_RULES = """
Create a compact evidence bundle for a recruiting-workflow outreach decision.
Use only the supplied provider-returned search passages. Never use prior knowledge or infer an internal problem,
dissatisfaction, buying intent, hiring acceleration, or a complete hiring count.
Keep historical evidence historical. Attribute personal statements, company statements,
and job-description expectations to their actual speaker or publisher. Missing evidence is
unknown, not false. Include only findings that could help assess relevance later.
Every finding must cite one or more supplied result IDs and include a short supporting passage.
If a research area lacks useful supported evidence, record a concise gap instead.
""".strip()


def _prompt(prospect: Prospect, pages: list[RetrievedPage]) -> str:
    chunks = []
    for page in pages:
        if not page.usable:
            continue
        chunks.append(
            f"SEARCH RESULT {page.page_id}\nURL: {page.url}\nTITLE: {page.title or ''}\n"
            f"AREAS: {', '.join(area.value for area in page.areas)}\nCONTENT:\n{page.content}"
        )
    sources = "\n\n---\n\n".join(chunks)
    return (
        f"{EXTRACTION_RULES}\n\n"
        f"Prospect: {prospect.name}\nCompany: {prospect.company}\n"
        f"Supplied role: {prospect.role}\n\nSOURCES\n{sources}"
    )


class GeminiExtractor:
    def __init__(self, model: str) -> None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("Missing GEMINI_API_KEY. Add it to .env or the process environment.")
        self.model = os.environ.get("GEMINI_MODEL", model)
        self.client = genai.Client(api_key=api_key)

    def _extract_sync(self, prospect: Prospect, pages: list[RetrievedPage]) -> tuple[EvidenceBundle, dict]:
        interaction = self.client.interactions.create(
            model=self.model,
            input=_prompt(prospect, pages),
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": EvidenceBundle.model_json_schema(),
            },
        )
        bundle = EvidenceBundle.model_validate_json(interaction.output_text)
        usage = {}
        raw_usage = getattr(interaction, "usage", None)
        if raw_usage is not None:
            usage = raw_usage.model_dump(mode="json") if hasattr(raw_usage, "model_dump") else {"value": str(raw_usage)}
        return bundle, usage

    async def extract(
        self, prospect: Prospect, pages: list[RetrievedPage]
    ) -> tuple[EvidenceBundle, CallRecord]:
        started_at, started_clock = datetime.now(UTC), perf_counter()
        try:
            bundle, usage = await asyncio.to_thread(self._extract_sync, prospect, pages)
            for index, finding in enumerate(bundle.findings, start=1):
                finding.finding_id = f"F{index:02d}"
            return bundle, CallRecord(
                operation="extract",
                request_label=f"{prospect.id}-evidence",
                started_at=started_at,
                duration_ms=round((perf_counter() - started_clock) * 1000),
                success=True,
                usage=usage,
            )
        except Exception as exc:
            return EvidenceBundle(), CallRecord(
                operation="extract",
                request_label=f"{prospect.id}-evidence",
                started_at=started_at,
                duration_ms=round((perf_counter() - started_clock) * 1000),
                success=False,
                error=str(exc)[:500],
            )


class NoopExtractor:
    async def extract(
        self, prospect: Prospect, pages: list[RetrievedPage]
    ) -> tuple[EvidenceBundle, CallRecord]:
        return EvidenceBundle(), CallRecord(
            operation="extract",
            request_label=f"{prospect.id}-evidence",
            started_at=datetime.now(UTC),
            duration_ms=0,
            success=True,
            usage={"skipped": True},
        )
