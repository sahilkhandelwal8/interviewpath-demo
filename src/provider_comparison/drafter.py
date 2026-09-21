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


PROMPT_PATH = ROOT / "prompts" / "outreach-drafter.md"


class OutreachDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    subject: str = Field(min_length=1)
    body: str = Field(min_length=1)
    selection_reason: str = Field(min_length=1)
    source_ids: list[str]

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
    model_config = ConfigDict(extra="forbid")
    draft: OutreachDraft | None
    review: ReviewMetadata

    @model_validator(mode="before")
    @classmethod
    def append_fixed_signature(cls, value):
        # Sender formatting belongs to the application. Preserve the raw model
        # response separately, and never turn a blank body into a valid draft.
        if not isinstance(value, dict) or not isinstance(value.get("draft"), dict):
            return value
        body = value["draft"].get("body")
        if not isinstance(body, str) or not body.strip():
            return value
        body = body.rstrip()
        signature = "Maya Chen\nInterviewPath"
        if body.endswith(signature):
            return value
        if body.endswith("Maya Chen"):
            body += "\nInterviewPath"
        else:
            body += "\n\n" + signature
        return {**value, "draft": {**value["draft"], "body": body}}


def validate_review(raw_response: str, draft_input: dict[str, Any]) -> ReviewResponse:
    response = ReviewResponse.model_validate_json(raw_response)
    if response.draft is None:
        if not response.review.limitation or response.review.claims:
            raise ValueError("A withheld draft requires a limitation and no draft claims")
        return response
    response.draft = validate_draft(response.draft.model_dump_json(), draft_input)
    passages = {s["source_id"]: s["passage"] for s in draft_input["research_bundle"]["source_passages"]}
    text = response.draft.subject + "\n" + response.draft.body
    for claim in response.review.claims:
        if claim.source_id not in response.draft.source_ids or claim.quote not in passages.get(claim.source_id, ""):
            raise ValueError("Review references unavailable evidence")
        if not claim.quote.strip() or claim.claim not in text:
            raise ValueError("Review claim must occur in the draft")
    if set(response.draft.source_ids) != {c.source_id for c in response.review.claims}:
        raise ValueError("Every cited source must map to a draft claim")
    if not response.review.claims and not response.review.limitation:
        raise ValueError("Unpersonalized drafts require an evidence limitation")
    if response.review.strength() == "Weak" and not response.review.limitation:
        raise ValueError("Weak evidence requires a visible limitation")
    return response


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
    draft = OutreachDraft.model_validate_json(raw_response)
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
