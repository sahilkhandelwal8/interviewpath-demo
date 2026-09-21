from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import ResearchArea, SearchResult


TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_KEYS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def select_results(results: list[SearchResult], max_pages: int) -> list[SearchResult]:
    """Round-robin areas and ranks so one verbose query cannot consume the page budget."""
    by_area = {
        area: sorted((item for item in results if item.area == area), key=lambda item: item.rank)
        for area in ResearchArea
    }
    selected: list[SearchResult] = []
    seen: set[str] = set()
    rank_index = 0
    while len(selected) < max_pages:
        added = False
        for area in ResearchArea:
            candidates = by_area[area]
            if rank_index >= len(candidates):
                continue
            item = candidates[rank_index]
            key = canonical_url(item.url)
            if key not in seen:
                selected.append(item)
                seen.add(key)
                added = True
                if len(selected) == max_pages:
                    break
        if not added and all(rank_index + 1 >= len(items) for items in by_area.values()):
            break
        rank_index += 1
    return selected

