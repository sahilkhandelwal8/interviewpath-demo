from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class ResearchArea(StrEnum):
    PROFESSIONAL_PROFILE = "professional_profile"
    COMPANY_DEVELOPMENTS = "company_developments"
    RECRUITING_CONTEXT = "recruiting_context"
    HIRING_FOOTPRINT = "hiring_footprint"


class Prospect(BaseModel):
    id: str
    name: str
    company: str
    role: str
    profile_url: str | None = None


class Query(BaseModel):
    id: str
    area: ResearchArea
    text: str
    round: int = 1


class QueryPlan(BaseModel):
    schema_version: str = "1.0"
    created_at: datetime
    prospects: list[Prospect]
    queries: dict[str, list[Query]]


class SearchResult(BaseModel):
    area: ResearchArea
    query_id: str
    rank: int
    url: str
    title: str | None = None
    snippet: str | None = None
    published_at: str | None = None


class CallRecord(BaseModel):
    operation: Literal["search", "retrieve", "extract", "draft", "identify"]
    request_label: str
    started_at: datetime
    duration_ms: int
    success: bool
    status_code: int | None = None
    error: str | None = None
    request_id: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)


class RetrievedPage(BaseModel):
    page_id: str
    url: str
    title: str | None = None
    areas: list[ResearchArea]
    content: str = ""
    published_at: str | None = None
    retrieved_at: datetime
    usable: bool
    error: str | None = None


class Finding(BaseModel):
    finding_id: str = ""
    area: ResearchArea
    fact: str = Field(description="A precise fact or clearly attributed public claim.")
    source_page_ids: list[str]
    supporting_passage: str
    publisher_or_author: str | None = None
    publication_or_event_date: str | None = None
    material_limitation: str | None = None
    evidence_kind: Literal["verified_fact", "attributed_claim"]


class EvidenceGap(BaseModel):
    area: ResearchArea
    description: str


class EvidenceBundle(BaseModel):
    findings: list[Finding] = Field(default_factory=list)
    gaps: list[EvidenceGap] = Field(default_factory=list)


class RunArtifact(BaseModel):
    schema_version: str = "1.0"
    run_id: str
    repetition: int
    provider: Literal["firecrawl", "exa"]
    prospect: Prospect
    started_at: datetime
    completed_at: datetime
    status: Literal["completed", "partial", "failed", "timed_out"]
    queries: list[Query]
    search_results: list[SearchResult]
    selected_urls: list[str]
    pages: list[RetrievedPage]
    evidence: EvidenceBundle
    calls: list[CallRecord]
    total_duration_ms: int
    provider_duration_ms: int
    extraction_duration_ms: int
    shared_enrichment: dict[str, Any] = Field(default_factory=dict)


class ClaimReview(BaseModel):
    finding_id: str
    supported: bool
    reference_fact_ids: list[str] = Field(default_factory=list)
    source_quality: Literal["first_party", "authoritative_secondary", "other", "unusable"]
    critical_error: str | None = None
    novel_useful: bool = False


class OutputReview(BaseModel):
    label: Literal["A", "B"]
    claim_reviews: list[ClaimReview] = Field(default_factory=list)
    covered_areas: list[ResearchArea] = Field(default_factory=list)


class PairReview(BaseModel):
    prospect_id: str
    repetition: int
    outputs: list[OutputReview]
    preferred: Literal["A", "B", "tie"]
    reason: str
