"""Firecrawl lookup, evidence presentation and explicit user-confirmed handoff."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
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
from .response_format import model_json


IDENTITY_FALLBACK_MODEL = "gemini-3.5-flash"
logger = logging.getLogger(__name__)


class EvidenceRef(BaseModel):
    source_id: str
    quote: str = ""


class MatchedField(BaseModel):
    value: str | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)


class Candidate(BaseModel):
    name: MatchedField
    company: MatchedField
    role: MatchedField
    note: str = ""
    company_conflict_evidence: list[EvidenceRef] = Field(default_factory=list,
        description="Exact quotes listing a different current employer from the candidate company, when supplied evidence does not explain the relationship. Empty only when no unresolved employer discrepancy exists.")

    @property
    def can_confirm(self) -> bool:
        return not self.company_conflict_evidence and all(
            f.value and f.value.strip() for f in (self.name, self.company, self.role))

    @property
    def has_confirmable_fields(self) -> bool:
        return all(f.value and f.value.strip() for f in (self.name, self.company, self.role))


class MatchSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")
    candidates: list[Candidate] = Field(max_length=3)
    summary: str = "Review the identity details against the available sources."
    explanation_evidence: list[EvidenceRef] = Field(default_factory=list,
        description="Sources and exact quotes supporting summary or candidate notes, including discrepancies")


class Confirmation(BaseModel):
    candidate_index: int = Field(ge=0)
    lookup_fingerprint: str
    confirmed_at: datetime
    confirmed_by: str = Field(min_length=1)
    resolved_prospect: Prospect | None = None


class IdentityLookup(BaseModel):
    lookup_id: str
    supplied: Prospect
    created_at: datetime
    query: Query | None = None
    sources: list[RetrievedPage] = Field(default_factory=list)
    result: MatchSummary | None = None
    calls: list[CallRecord] = Field(default_factory=list)
    model: str
    generation_config: dict[str, Any] = Field(default_factory=lambda: {"temperature": 0.2})
    system_prompt: str
    raw_response: str | None = None
    confirmation: Confirmation | None = None


class IdentityEvidenceError(ValueError):
    """The model's match cannot be supported by the supplied passages."""


def reconcile_evidence(result: MatchSummary, sources: list[RetrievedPage]) -> MatchSummary:
    """Recover presentation differences, then retain only source-backed fields.

    This runs once on model output, never on a previously confirmed handoff.
    Recovered quotations use the original passage, including omitted qualifiers.
    The raw model response remains in the lookup artifact for inspection.
    """
    result = result.model_copy(deep=True)
    source_ids = {s.page_id for s in sources}

    def reconcile(refs):
        supported = []
        for ref in refs:
            if ref.source_id in source_ids:
                supported.append(ref)
        return supported

    explanations = reconcile(result.explanation_evidence)
    lost_explanation = len(explanations) != len(result.explanation_evidence)
    result.explanation_evidence = explanations
    for candidate in result.candidates:
        candidate.company_conflict_evidence = reconcile(candidate.company_conflict_evidence)
        for field in (candidate.name, candidate.company, candidate.role):
            field.evidence = reconcile(field.evidence) if field.value and field.value.strip() else []
    if lost_explanation:
        result.summary = "Review the person, company and role against the supporting sources." if result.candidates else "The available sources did not establish a match. Check the details or add a profile link."
    return result


def validate_matches(result: MatchSummary, sources: list[RetrievedPage]) -> None:
    source_ids = {s.page_id for s in sources}
    references = list(result.explanation_evidence)
    for candidate in result.candidates:
        references.extend(candidate.company_conflict_evidence)
        for field in (candidate.name, candidate.company, candidate.role):
            if field.value is not None and not field.value.strip():
                raise IdentityEvidenceError("Empty matched field")
            references.extend(field.evidence)
    for ref in references:
        if ref.source_id not in source_ids:
            raise IdentityEvidenceError("Match cites an unavailable source or unsupported quotation")


def fingerprint(lookup: IdentityLookup) -> str:
    payload = lookup.model_dump(mode="json", exclude={"confirmation"})
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def confirm_match(lookup: IdentityLookup, candidate_index: int, *, confirmed_by: str,
                  allow_employer_conflict: bool = False,
                  resolved_prospect: Prospect | None = None) -> IdentityLookup:
    if not confirmed_by.strip():
        raise ValueError("The confirming user is required")
    # A failed primary attempt is allowed when a subsequent fallback succeeds.
    if lookup.result is None or not lookup.calls or not lookup.calls[-1].success:
        raise ValueError("Lookup failed; run the lookup again before confirming")
    validate_matches(lookup.result, lookup.sources)
    if not 0 <= candidate_index < len(lookup.result.candidates):
        raise ValueError("Select an available candidate")
    candidate = lookup.result.candidates[candidate_index]
    if not candidate.can_confirm and not (allow_employer_conflict and candidate.has_confirmable_fields):
        raise ValueError("Name, current company and role need evidence without unresolved employer differences; edit details and search again")
    if resolved_prospect and any(not getattr(resolved_prospect, field).strip() for field in ("name", "company", "role")):
        raise ValueError("Resolved prospect needs a name, company and role")
    return lookup.model_copy(update={"confirmation": Confirmation(
        candidate_index=candidate_index, lookup_fingerprint=fingerprint(lookup),
        confirmed_at=datetime.now(UTC), confirmed_by=confirmed_by.strip(),
        resolved_prospect=resolved_prospect,
    )}, deep=True)


def confirmed_prospect(lookup: IdentityLookup) -> Prospect:
    confirmation = lookup.confirmation
    if confirmation is None or confirmation.lookup_fingerprint != fingerprint(lookup):
        raise ValueError("User confirmation is missing or stale; confirm the current lookup")
    # Recheck source validity and selection when loading a saved handoff.
    confirm_match(lookup, confirmation.candidate_index, confirmed_by=confirmation.confirmed_by,
                  allow_employer_conflict=True)
    if confirmation.resolved_prospect:
        return confirmation.resolved_prospect
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
        "candidates": [dict(index=i, **c.model_dump(mode="json"), can_confirm=bool(healthy and c.can_confirm),
                            can_confirm_with_clarification=bool(healthy and c.has_confirmable_fields))
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
        if client is None:
            client = genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options={
                "timeout": settings.identity_timeout_seconds * 1000, "retry_options": {"attempts": 1}})
            client.aio.interactions.sdk_configuration.retry_config = None

        payload = json.dumps({"supplied": lookup.supplied.model_dump(mode="json"),
                              "sources": [s.model_dump(mode="json", exclude={"title"}) for s in lookup.sources]}, ensure_ascii=False)
        logger.info("identity.gemini.input prospect=%s sources=%d payload=%s", lookup.supplied.id, len(lookup.sources), payload)
        models = [lookup.model]
        for model in models:
            started_at, clock = datetime.now(UTC), perf_counter()
            call = CallRecord(operation="identify", request_label=f"{lookup.supplied.id}-match",
                              started_at=started_at, duration_ms=0, success=False)
            try:
                logger.info("identity.gemini.request prospect=%s model=%s", lookup.supplied.id, model)
                response = await asyncio.wait_for(client.aio.interactions.create(
                    model=model, system_instruction=lookup.system_prompt, input=payload,
                    generation_config=lookup.generation_config, store=False,
                    response_format={"type": "text", "mime_type": "application/json",
                                     "schema": MatchSummary.model_json_schema()}),
                    timeout=settings.identity_timeout_seconds)
                lookup.raw_response = response.output_text
                logger.info("identity.gemini.output prospect=%s model=%s status=%s output=%s", lookup.supplied.id, model, getattr(response, "status", None), lookup.raw_response)
                call.request_id = getattr(response, "id", None)
                usage = getattr(response, "usage", None)
                call.usage = usage.model_dump(mode="json") if hasattr(usage, "model_dump") else dict(usage or {})
                if getattr(response, "status", "completed") != "completed":
                    raise ValueError("Incomplete model response")
                result = reconcile_evidence(MatchSummary.model_validate(model_json(lookup.raw_response)), lookup.sources)
                validate_matches(result, lookup.sources)
                lookup.result = result
                lookup.model = model
                call.success = True
                break
            except IdentityEvidenceError:
                logger.exception("identity.validation.failed prospect=%s model=%s", lookup.supplied.id, model)
                call.error = "Identity response failed evidence validation: a field or quotation was not supported by the supplied passages; no automatic retry was made"
                if model == lookup.model and IDENTITY_FALLBACK_MODEL != model:
                    models.append(IDENTITY_FALLBACK_MODEL)
            except Exception as exc:
                logger.exception("identity.gemini.failed prospect=%s model=%s", lookup.supplied.id, model)
                detail = str(exc).lower()
                status = getattr(exc, "status_code", None) or getattr(exc, "code", None) or getattr(exc, "status", None)
                unavailable = status in (429, 503) or "429" in detail or "503" in detail or "rate limit" in detail or "service unavailable" in detail or "unavailable" in detail or "overloaded" in detail
                call.status_code = status if isinstance(status, int) and 400 <= status < 600 else (503 if unavailable else None)
                call.error = f"Gemini model {model} failed ({type(exc).__name__})" + ("; service unavailable" if unavailable else "")
                if model == lookup.model and IDENTITY_FALLBACK_MODEL != model:
                    models.append(IDENTITY_FALLBACK_MODEL)
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
        logger.info("identity.firecrawl.input prospect=%s query=%s limit=5", prospect.id, query.text)
        logger.info("identity.firecrawl.output prospect=%s count=%d results=%s", prospect.id, len(results), [r.model_dump(mode="json") for r in results])
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
        logger.info("identity.sources prospect=%s sources=%s", prospect.id, [s.model_dump(mode="json") for s in lookup.sources])
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
