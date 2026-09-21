from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, date, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from google import genai
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .config import ROOT
from .models import CallRecord, RunArtifact
from .selection import canonical_url
from .review import ReviewMetadata, REVIEW_INSTRUCTIONS
from .response_format import model_json, source_quote, normalized
from .review import ClaimSupport


PROMPT_PATH = ROOT / "prompts" / "outreach-drafter.md"


class OutreachDraft(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    subject: str = Field(min_length=1)
    body: str = Field(min_length=1)
    selection_reason: str = Field(default="Review the message and its supporting sources.", min_length=1)
    source_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_formatting(cls, value):
        if not isinstance(value, dict):
            return value
        value = dict(value)
        if isinstance(value.get("subject"), str):
            value["subject"] = " ".join(value["subject"].split())
        if not isinstance(value.get("selection_reason"), str) or not value["selection_reason"].strip():
            value["selection_reason"] = "Review the message and its supporting sources."
        if isinstance(value.get("source_ids"), str):
            value["source_ids"] = [value["source_ids"]] if value["source_ids"].strip() else []
        if value.get("source_ids") is None:
            value["source_ids"] = []
        body = value.get("body")
        if isinstance(body, str) and body.strip():
            body = body.replace("\r\n", "\n").replace("\r", "\n").rstrip()
            if not body.endswith("Maya Chen\nInterviewPath"):
                body += "\nInterviewPath" if body.endswith("Maya Chen") else "\n\nMaya Chen\nInterviewPath"
            value["body"] = body
        return value

    @field_validator("subject", "body", "selection_reason")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Draft text must not be blank")
        return value

    @field_validator("subject")
    @classmethod
    def single_line(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("Subject must be a single line")
        return value

    @field_validator("body")
    @classmethod
    def required_signature(cls, value: str) -> str:
        if not value.rstrip().endswith("Maya Chen\nInterviewPath"):
            raise ValueError("Body must end with the required sender signature")
        return value


class ReviewResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    draft: OutreachDraft | None
    review: ReviewMetadata



def validate_review(raw_response: str, draft_input: dict[str, Any]) -> ReviewResponse:
    payload = model_json(raw_response)
    if not isinstance(payload, dict):
        raise ValueError("The model did not return a draft object")
    if "draft" not in payload and "subject" in payload and "body" in payload:
        payload = {"draft": payload}
    raw_review = payload.get("review") if isinstance(payload.get("review"), dict) else {}
    metadata = {key: raw_review[key] for key in ("angle", "explanation", "limitation")
                if isinstance(raw_review.get(key), str) and raw_review[key].strip()}
    review = ReviewMetadata(**metadata)
    if payload.get("draft") is None:
        review.limitation = review.limitation or "The available evidence did not support a draft. Add a source or refine the instructions."
        return ReviewResponse(draft=None, review=review)
    raw_draft = payload["draft"]
    malformed_ids = False
    if isinstance(raw_draft, dict):
        raw_draft = dict(raw_draft)
        ids = raw_draft.get("source_ids")
        if ids is not None and not isinstance(ids, (list, str)):
            raw_draft["source_ids"], malformed_ids = [], True
        elif isinstance(ids, list) and any(not isinstance(item, str) for item in ids):
            raw_draft["source_ids"], malformed_ids = [item for item in ids if isinstance(item, str)], True
    draft = OutreachDraft.model_validate(raw_draft)
    available = {s["source_id"]: s for s in draft_input["research_bundle"]["source_passages"]}
    declared = set(draft.source_ids)
    incomplete = malformed_ids or bool(declared - available.keys())
    text = draft.subject + "\n" + draft.body
    raw_claims = raw_review.get("claims") or []
    if not isinstance(raw_claims, list):
        raw_claims, incomplete = [], True
    for item in raw_claims:
        if not isinstance(item, dict):
            incomplete = True
            continue
        item = dict(item)
        source = available.get(item.get("source_id")) if isinstance(item.get("source_id"), str) else None
        quote = source_quote(item.get("quote"), source["passage"]) if source else None
        if not quote or not isinstance(item.get("claim"), str) or not item["claim"].strip() or normalized(item["claim"]) not in normalized(text):
            incomplete = True
            continue
        item["quote"] = quote
        if not isinstance(item.get("publisher"), str) or not item["publisher"].strip():
            from urllib.parse import urlparse
            item["publisher"] = urlparse(source["url"]).hostname or "Source"
        for key, choices, fallback in [
            ("support", ["Direct", "Partial", "Unclear"], "Unclear"),
            ("source", ["First-party", "Authoritative secondary", "Index-only/other"], "Index-only/other"),
            ("timing", ["Dated and suitable", "Historical", "Undated/not material"], "Undated/not material"),
            ("consistency", ["No conflict found", "Material conflict"], "Material conflict"),
        ]:
            value = item.get(key)
            matched = next((choice for choice in choices if isinstance(value, str) and choice.lower() == value.strip().lower()), None)
            item[key] = matched or fallback
            if matched is None:
                incomplete = True
        try:
            review.claims.append(ClaimSupport.model_validate(item))
        except ValidationError:
            incomplete = True
    cited = {claim.source_id for claim in review.claims}
    incomplete = incomplete or bool(declared - cited)
    draft.source_ids = list(dict.fromkeys(claim.source_id for claim in review.claims))
    review.assessment_incomplete = incomplete
    if incomplete:
        warning = "Some claims could not be linked reliably to the selected evidence. Check or edit those claims before using this draft."
        review.limitation = warning + (" " + review.limitation if review.limitation else "")
    elif not review.claims:
        review.limitation = review.limitation or "No personalized claims were linked to source evidence. Review the message before using it."
    elif review.strength() == "Weak":
        review.limitation = review.limitation or "Some claims have uncertain or conflicting support. Check them against the sources before using the draft."
    return ReviewResponse(draft=draft, review=review)


class DraftArtifact(BaseModel):
    research_artifact: str
    model: str
    generation_config: dict[str, Any] = Field(default_factory=dict)
    system_prompt: str
    input: dict[str, Any]
    raw_response: str | None = None
    draft: OutreachDraft | None = None
    review: ReviewMetadata | None = None
    call: CallRecord


def load_system_prompt(path: Path = PROMPT_PATH) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.index("## System prompt\n") + len("## System prompt\n")
    end = text.index("## User-input template\n", start)
    prompt = text[start:end].strip()
    if not prompt:
        raise ValueError("Drafter system prompt is empty")
    return prompt


def build_draft_input(
    artifact: RunArtifact, verified_role: str, drafting_date: date,
    selected_source_ids: list[str] | None = None,
    additional_sources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Use existing selected search passages; never promote the supplied role to verified."""
    if not verified_role.strip():
        raise ValueError("A verified role or responsibility is required")
    if "identity" in artifact.shared_enrichment:
        from .identity import IdentityLookup, confirmed_prospect
        matched = confirmed_prospect(IdentityLookup.model_validate(artifact.shared_enrichment["identity"]))
        if artifact.prospect != matched or verified_role != matched.role:
            raise ValueError("Draft context differs from the user-confirmed identity")
    if any(call.operation == "retrieve" for call in artifact.calls):
        raise ValueError("Drafting requires a search-only research artifact, not retrieved pages")

    sources = []
    seen_urls: set[str] = set()
    seen_ids: set[str] = set()
    for page in artifact.pages:
        if not page.usable or not page.content.strip():
            continue
        url = canonical_url(page.url)
        if url in seen_urls:
            continue
        if not page.page_id or page.page_id in seen_ids:
            raise ValueError("Source IDs must be nonempty and unique")
        seen_urls.add(url)
        seen_ids.add(page.page_id)
        sources.append({
            "source_id": page.page_id,
            "url": page.url,
            "title": page.title,
            "passage": page.content[:2000],
            "published_at": page.published_at,
            "collected_at": page.retrieved_at.isoformat(),
            "evidence_kind": "search_passage",
        })
        if len(sources) == 8:
            break

    sources.extend(additional_sources or [])

    if selected_source_ids is not None:
        available = {source["source_id"] for source in sources}
        if (not isinstance(selected_source_ids, list) or not selected_source_ids
                or any(not isinstance(source_id, str) for source_id in selected_source_ids)
                or set(selected_source_ids) - available):
            raise ValueError("Choose at least one available research source.")
        sources = [source for source in sources if source["source_id"] in selected_source_ids]
    passages = {source["source_id"]: source["passage"] for source in sources}
    # Do not let extracted summaries reintroduce evidence outside the bounded context.
    findings = [
        finding.model_dump(mode="json", exclude={"supporting_passage"})
        for finding in artifact.evidence.findings
        if finding.source_page_ids
        and all(source_id in passages for source_id in finding.source_page_ids)
        and finding.supporting_passage.strip()
        and any(finding.supporting_passage in passages[source_id] for source_id in finding.source_page_ids)
    ]
    return {
        "drafting_date": drafting_date.isoformat(),
        "prospect_context": {
            "person_name": artifact.prospect.name,
            "company": artifact.prospect.company,
            "verified_role_or_responsibility": verified_role.strip(),
        },
        "research_bundle": {
            "status": artifact.status,
            "findings": findings,
            "gaps": [gap.model_dump(mode="json") for gap in artifact.evidence.gaps],
            "source_passages": sources,
        },
    }


def validate_draft(raw_response: str, draft_input: dict[str, Any]) -> OutreachDraft:
    draft = OutreachDraft.model_validate(model_json(raw_response))
    available = {
        source["source_id"] for source in draft_input["research_bundle"]["source_passages"]
    }
    if set(draft.source_ids) - available:
        raise ValueError("Draft cites source IDs absent from its supplied evidence")
    # Duplicate references add no information; preserve the model's first-use order.
    draft.source_ids = list(dict.fromkeys(draft.source_ids))
    return draft


class GeminiDrafter:
    def __init__(
        self, model: str, timeout_seconds: int = 30, *, temperature: float = 0.7,
        client: Any = None,
    ) -> None:
        self.model = os.environ.get("GEMINI_MODEL", model)
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature
        if client is None:
            api_key = os.environ.get("GEMINI_API_KEY")
            if not api_key:
                raise RuntimeError("Missing GEMINI_API_KEY. Add it to .env or the process environment.")
            client = genai.Client(
                api_key=api_key,
                http_options={
                    "timeout": timeout_seconds * 1000,
                    "retry_options": {"attempts": 1},
                },
            )
            # The installed SDK coerces attempts=0 to 1, then interprets that
            # as one retry in Interactions. Disable retries on that resource
            # explicitly; the mocked HTTP regression test checks request count.
            client.aio.interactions.sdk_configuration.retry_config = None
        self.client = client

    async def draft(
        self,
        artifact: RunArtifact,
        *,
        research_artifact: str,
        verified_role: str,
        drafting_date: date | None = None,
        with_review: bool = False,
        regeneration: dict[str, Any] | None = None,
        additional_sources: list[dict[str, Any]] | None = None,
    ) -> DraftArtifact:
        system_prompt = load_system_prompt()
        if with_review:
            system_prompt += "\n\n" + REVIEW_INSTRUCTIONS
        draft_input = build_draft_input(
            artifact, verified_role, drafting_date or datetime.now(UTC).date(),
            selected_source_ids=(regeneration or {}).get("selected_source_ids"),
            additional_sources=additional_sources,
        )
        if regeneration is not None:
            draft_input["regeneration"] = regeneration
            system_prompt += """\n\nThis is a regeneration: the previous draft was not satisfactory to the user.
Use regeneration.previous_draft (including the user's edits) and follow
regeneration.user_comments when provided to write a better replacement.
If comments are empty, improve the previous message's relevance, clarity and specificity.
The previous draft and user comments are revision context, NOT verified evidence.
Do not carry unsupported claims forward. All factual personalization must still
be grounded in research_bundle. Preserve the product boundaries and fixed sender.
Only the supplied source passages are allowed as evidence. If a previous claim
is no longer supported by these passages, remove it. Selecting a source permits
its use; it does not require a claim from every selected source.
Return the full replacement draft and a fresh review of that replacement in one response.
"""
        started_at, started_clock = datetime.now(UTC), perf_counter()
        raw_response = None
        draft = None
        review = None
        usage: dict[str, Any] = {}
        request_id = None
        error = None
        try:
            interaction = await asyncio.wait_for(
                self.client.aio.interactions.create(
                    model=self.model,
                    system_instruction=system_prompt,
                    input=json.dumps(draft_input, ensure_ascii=False),
                    generation_config={"temperature": self.temperature},
                    response_format={
                        "type": "text",
                        "mime_type": "application/json",
                        "schema": (ReviewResponse if with_review else OutreachDraft).model_json_schema(),
                    },
                    store=False,
                ),
                timeout=self.timeout_seconds,
            )
            request_id = getattr(interaction, "id", None)
            raw_response = interaction.output_text
            raw_usage = getattr(interaction, "usage", None)
            if raw_usage is not None:
                usage = raw_usage.model_dump(mode="json") if hasattr(raw_usage, "model_dump") else dict(raw_usage)
            if getattr(interaction, "status", "completed") != "completed":
                raise ValueError("Model interaction did not complete")
            if with_review:
                response = validate_review(raw_response, draft_input)
                draft, review = response.draft, response.review
            else:
                draft = validate_draft(raw_response, draft_input)
        except TimeoutError:
            error = "Drafting timed out; no automatic retry was made"
        except ValidationError:
            error = "Draft response failed schema validation; no automatic retry was made"
        except ValueError:
            error = "Draft response failed content or evidence validation; no automatic retry was made"
        except Exception as exc:
            # Do not persist arbitrary SDK exception text, which may contain credentials.
            error = f"Drafting failed ({type(exc).__name__}); inspect the saved response if present"
        return DraftArtifact(
            research_artifact=research_artifact,
            model=self.model,
            generation_config={"temperature": self.temperature},
            system_prompt=system_prompt,
            input=draft_input,
            raw_response=raw_response,
            draft=draft,
            review=review,
            call=CallRecord(
                operation="draft",
                request_label=f"{artifact.prospect.id}-draft",
                started_at=started_at,
                duration_ms=round((perf_counter() - started_clock) * 1000),
                success=draft is not None or review is not None,
                request_id=request_id,
                usage=usage,
                error=error,
            ),
        )

    async def close(self) -> None:
        await self.client.aio.aclose()
        self.client.close()
