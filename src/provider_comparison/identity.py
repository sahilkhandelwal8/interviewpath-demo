"""Firecrawl lookup, evidence presentation and explicit user-confirmed handoff."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from google import genai
from pydantic import BaseModel, ConfigDict, Field

from .config import ROOT, Settings
from .models import CallRecord, Prospect, Query, ResearchArea, RetrievedPage
from .providers import create_provider
from .selection import canonical_url


class EvidenceRef(BaseModel):
    source_id: str
    quote: str = Field(min_length=1)


class MatchedField(BaseModel):
    value: str | None
    evidence: list[EvidenceRef]


class Candidate(BaseModel):
    name: MatchedField
    company: MatchedField
    role: MatchedField
    note: str
    company_conflict_evidence: list[EvidenceRef] = Field(default_factory=list,
        description="Exact quotes listing a different current employer from the candidate company, when supplied evidence does not explain the relationship. Empty only when no unresolved employer discrepancy exists.")

    @property
    def can_confirm(self) -> bool:
        return not self.company_conflict_evidence and all(
            f.value and f.value.strip() and f.evidence for f in (self.name, self.company, self.role))


class MatchSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidates: list[Candidate] = Field(max_length=3)
    summary: str = Field(min_length=1)
    explanation_evidence: list[EvidenceRef] = Field(default_factory=list,
        description="Sources and exact quotes supporting summary or candidate notes, including discrepancies")


class Confirmation(BaseModel):
    candidate_index: int = Field(ge=0)
    lookup_fingerprint: str
    confirmed_at: datetime
    confirmed_by: str = Field(min_length=1)


class IdentityLookup(BaseModel):
    lookup_id: str
    supplied: Prospect
    created_at: datetime
    query: Query | None = None
    sources: list[RetrievedPage] = Field(default_factory=list)
    result: MatchSummary | None = None
    calls: list[CallRecord] = Field(default_factory=list)
    model: str
    generation_config: dict[str, Any] = Field(default_factory=lambda: {"temperature": 0})
    system_prompt: str
    raw_response: str | None = None
    confirmation: Confirmation | None = None


class IdentityEvidenceError(ValueError):
    """The model's match cannot be supported by the supplied passages."""


def validate_matches(result: MatchSummary, sources: list[RetrievedPage]) -> None:
    passages = {s.page_id: s.content for s in sources if s.usable}
    references = list(result.explanation_evidence)
    for candidate in result.candidates:
        references.extend(candidate.company_conflict_evidence)
        for field in (candidate.name, candidate.company, candidate.role):
            if field.value is not None and not field.value.strip():
                raise IdentityEvidenceError("Empty matched field")
            if bool(field.value) != bool(field.evidence):
                raise IdentityEvidenceError("Each populated field needs evidence; unknown fields must have none")
            references.extend(field.evidence)
    for ref in references:
        if not ref.quote.strip() or ref.quote not in passages.get(ref.source_id, ""):
            raise IdentityEvidenceError("Match cites an unavailable source or unsupported quotation")


def fingerprint(lookup: IdentityLookup) -> str:
    payload = lookup.model_dump(mode="json", exclude={"confirmation"})
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def confirm_match(lookup: IdentityLookup, candidate_index: int, *, confirmed_by: str) -> IdentityLookup:
    if not confirmed_by.strip():
        raise ValueError("The confirming user is required")
    if lookup.result is None or not lookup.calls or any(not c.success for c in lookup.calls):
        raise ValueError("Lookup failed; run the lookup again before confirming")
    validate_matches(lookup.result, lookup.sources)
    if not 0 <= candidate_index < len(lookup.result.candidates):
        raise ValueError("Select an available candidate")
    if not lookup.result.candidates[candidate_index].can_confirm:
        raise ValueError("Name, current company and role need evidence without unresolved employer differences; edit details and search again")
    return lookup.model_copy(update={"confirmation": Confirmation(
        candidate_index=candidate_index, lookup_fingerprint=fingerprint(lookup),
        confirmed_at=datetime.now(UTC), confirmed_by=confirmed_by.strip(),
    )}, deep=True)


def confirmed_prospect(lookup: IdentityLookup) -> Prospect:
    confirmation = lookup.confirmation
    if confirmation is None or confirmation.lookup_fingerprint != fingerprint(lookup):
        raise ValueError("User confirmation is missing or stale; confirm the current lookup")
    # Recheck source validity and selection when loading a saved handoff.
    confirm_match(lookup, confirmation.candidate_index, confirmed_by=confirmation.confirmed_by)
    candidate = lookup.result.candidates[confirmation.candidate_index]
    return Prospect(id=lookup.supplied.id, name=candidate.name.value,
                    company=candidate.company.value, role=candidate.role.value,
                    profile_url=lookup.supplied.profile_url)


def presentation(lookup: IdentityLookup) -> dict[str, Any]:
    """UI-ready view; confirmation applies only to the three input fields."""
    confirmed = False
    if lookup.confirmation:
        try:
            confirmed_prospect(lookup)
            confirmed = True
        except ValueError:
            pass
    healthy = bool(lookup.result and lookup.calls and all(c.success for c in lookup.calls))
    cited_ids = {ref.source_id for candidate in (lookup.result.candidates if lookup.result else [])
                 for field in (candidate.name, candidate.company, candidate.role) for ref in field.evidence}
    if lookup.result:
        cited_ids.update(ref.source_id for ref in lookup.result.explanation_evidence)
        cited_ids.update(ref.source_id for candidate in lookup.result.candidates for ref in candidate.company_conflict_evidence)
    return {
        "lookup_id": lookup.lookup_id,
        "entered": lookup.supplied.model_dump(mode="json"),
        "summary": lookup.result.summary if lookup.result else "We couldn't complete the lookup. Please try again.",
        "explanation_evidence": [ref.model_dump() for ref in lookup.result.explanation_evidence] if lookup.result else [],
        "candidates": [dict(index=i, **c.model_dump(mode="json"), can_confirm=bool(healthy and c.can_confirm))
                       for i, c in enumerate(lookup.result.candidates if lookup.result else [])],
        "sources": [s.model_dump(mode="json") for s in lookup.sources if s.page_id in cited_ids],
        "confirmation_label": "Confirmed by you" if confirmed else None,
        "confirmed_candidate_index": lookup.confirmation.candidate_index if confirmed else None,
        "actions": ["confirm", "edit_details", "cancel"] if healthy and any(c.can_confirm for c in lookup.result.candidates)
                   else ["edit_details", "retry", "cancel"],
    }


async def summarize_lookup(lookup: IdentityLookup, settings: Settings, *, client=None) -> IdentityLookup:
    """Summarize already collected sources, preserving their original dates and IDs.

    A new summary is always unconfirmed. Prior artifacts remain unchanged.
    """
    lookup = lookup.model_copy(deep=True)
    lookup.lookup_id = os.urandom(12).hex()
    lookup.confirmation = None
    lookup.result = None
    lookup.raw_response = None
    lookup.calls = [c for c in lookup.calls if c.operation == "search"]
    lookup.system_prompt = (ROOT / "prompts/prospect-matcher.md").read_text(encoding="utf-8")
    owns_client = client is None
    try:
        started_at, clock = datetime.now(UTC), perf_counter()
        call = CallRecord(operation="identify", request_label=f"{lookup.supplied.id}-match",
                          started_at=started_at, duration_ms=0, success=False)
        try:
            if client is None:
                client = genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options={
                    "timeout": settings.draft_timeout_seconds * 1000, "retry_options": {"attempts": 1}})
                client.aio.interactions.sdk_configuration.retry_config = None
            response = await asyncio.wait_for(client.aio.interactions.create(
                model=lookup.model, system_instruction=lookup.system_prompt,
                input=json.dumps({"supplied": lookup.supplied.model_dump(mode="json"),
                                  # Titles stay in the audit/UI but cannot be quoted as passage evidence.
                                  "sources": [s.model_dump(mode="json", exclude={"title"}) for s in lookup.sources]}, ensure_ascii=False),
                generation_config=lookup.generation_config, store=False,
                response_format={"type": "text", "mime_type": "application/json",
                                 "schema": MatchSummary.model_json_schema()}),
                timeout=settings.draft_timeout_seconds)
            lookup.raw_response = response.output_text
            call.request_id = getattr(response, "id", None)
            usage = getattr(response, "usage", None)
            call.usage = usage.model_dump(mode="json") if hasattr(usage, "model_dump") else dict(usage or {})
            if getattr(response, "status", "completed") != "completed":
                raise ValueError("Incomplete model response")
            result = MatchSummary.model_validate_json(lookup.raw_response)
            validate_matches(result, lookup.sources)
            lookup.result = result
            call.success = True
        except IdentityEvidenceError:
            call.error = "Identity response failed evidence validation: a field or quotation was not supported by the supplied passages; no automatic retry was made"
        except Exception as exc:
            call.error = f"Match summary failed ({type(exc).__name__}); no automatic retry was made"
        finally:
            call.duration_ms = round((perf_counter()-clock)*1000)
            lookup.calls.append(call)
        return lookup
    finally:
        if owns_client and client is not None:
            await client.aio.aclose()
            client.close()


async def lookup_prospect(prospect: Prospect, settings: Settings, *, provider=None, client=None) -> IdentityLookup:
    if any(not getattr(prospect, field).strip() for field in ("id", "name", "company")):
        raise ValueError("Name and company are required")
    model = os.environ.get("GEMINI_MODEL", settings.gemini_model)
    prompt = (ROOT / "prompts/prospect-matcher.md").read_text(encoding="utf-8")
    query = Query(id=f"{prospect.id}-identity", area=ResearchArea.PROFESSIONAL_PROFILE,
                  text=f'"{prospect.name}" "{prospect.company}" {prospect.role} {prospect.profile_url or ""}'.strip())
    lookup = IdentityLookup(lookup_id=os.urandom(12).hex(), supplied=prospect,
                            created_at=datetime.now(UTC), query=query, model=model, system_prompt=prompt)
    owns_provider = provider is None
    provider = provider or create_provider("firecrawl", base_url=settings.firecrawl_base_url,
                                           timeout_seconds=settings.request_timeout_seconds)
    try:
        results = await provider.search(query, 5)
        lookup.calls = list(provider.calls)
        seen = set()
        for result in results:
            url = canonical_url(result.url)
            if url in seen or not (result.snippet or "").strip():
                continue
            seen.add(url)
            lookup.sources.append(RetrievedPage(
                page_id=f"IDENTITY-{len(lookup.sources)+1:02d}", url=result.url,
                title=result.title, areas=[ResearchArea.PROFESSIONAL_PROFILE],
                content=result.snippet[:2000], published_at=result.published_at,
                retrieved_at=lookup.created_at, usable=True))
        if not lookup.sources:
            if lookup.calls and all(c.success for c in lookup.calls):
                lookup.result = MatchSummary(candidates=[], summary="No clear match found. Check the details or add a professional profile URL.")
            return lookup
        return await summarize_lookup(lookup, settings, client=client)
    finally:
        if owns_provider:
            await provider.close()


async def research_confirmed(lookup: IdentityLookup, settings: Settings, *, run_id: str, on_result=None):
    from .extractor import NoopExtractor
    from .pipeline import run_prospect
    from .queries import build_queries

    prospect = confirmed_prospect(lookup)  # No provider calls before this check.
    artifact = await run_prospect(run_id=run_id, repetition=1, provider_name="exa",
                                  prospect=prospect, queries=build_queries(prospect),
                                  settings=settings, extractor=NoopExtractor(), on_result=on_result)
    selected = lookup.result.candidates[lookup.confirmation.candidate_index]
    ids = {ref.source_id for field in (selected.name, selected.company, selected.role) for ref in field.evidence}
    ids.update(ref.source_id for ref in lookup.result.explanation_evidence)
    # Reuse identity passages, with distinct IDs; the drafter applies its usual context cap.
    artifact.pages = [s for s in lookup.sources if s.page_id in ids] + artifact.pages
    artifact.shared_enrichment["identity"] = lookup.model_dump(mode="json")
    return artifact
