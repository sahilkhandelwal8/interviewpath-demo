from __future__ import annotations

from datetime import UTC, datetime

from .models import Prospect, Query, QueryPlan, ResearchArea


def build_queries(prospect: Prospect) -> list[Query]:
    name = f'"{prospect.name}"'
    company = f'"{prospect.company}"'
    return [
        Query(
            id=f"{prospect.id}-profile-1",
            area=ResearchArea.PROFESSIONAL_PROFILE,
            text=f"{name} {company} {prospect.role} LinkedIn interview biography",
        ),
        Query(
            id=f"{prospect.id}-developments-1",
            area=ResearchArea.COMPANY_DEVELOPMENTS,
            text=f"{company} hiring expansion funding leadership office",
        ),
        Query(
            id=f"{prospect.id}-context-1",
            area=ResearchArea.RECRUITING_CONTEXT,
            text=f"{name} {company} recruiting hiring interview talent acquisition",
        ),
    ]


def build_plan(prospects: list[Prospect]) -> QueryPlan:
    return QueryPlan(
        created_at=datetime.now(UTC),
        prospects=prospects,
        queries={prospect.id: build_queries(prospect) for prospect in prospects},
    )
