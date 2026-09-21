from __future__ import annotations

import random
from pathlib import Path

from .config import read_json, write_json
from .models import RunArtifact


def _review_output(label: str, artifact: RunArtifact) -> dict:
    return {
        "label": label,
        "status": artifact.status,
        "prospect": artifact.prospect.model_dump(mode="json"),
        "evidence": artifact.evidence.model_dump(mode="json"),
        "sources": [
            {
                "page_id": page.page_id,
                "url": page.url,
                "title": page.title,
                "areas": [area.value for area in page.areas],
                "published_at": page.published_at,
                "usable": page.usable,
                "content": page.content,
            }
            for page in artifact.pages
        ],
    }


def create_blind_packets(run_dir: Path, seed: str = "zamp-v1") -> tuple[Path, Path]:
    artifacts: dict[tuple[str, int, str], RunArtifact] = {}
    for provider in ("firecrawl", "exa"):
        for path in (run_dir / provider).glob("*/run-*.json"):
            artifact = RunArtifact.model_validate(read_json(path))
            artifacts[(artifact.prospect.id, artifact.repetition, provider)] = artifact

    review_dir = run_dir / "review"
    private_dir = run_dir / "_private"
    mapping: dict[str, dict[str, str]] = {}
    pairs = sorted({(prospect_id, repetition) for prospect_id, repetition, _ in artifacts})
    for prospect_id, repetition in pairs:
        left = artifacts.get((prospect_id, repetition, "firecrawl"))
        right = artifacts.get((prospect_id, repetition, "exa"))
        if left is None or right is None:
            continue
        providers = ["firecrawl", "exa"]
        random.Random(f"{seed}:{prospect_id}:{repetition}").shuffle(providers)
        labels = {"A": providers[0], "B": providers[1]}
        pair_id = f"{prospect_id}-run-{repetition:02d}"
        mapping[pair_id] = labels
        packet = {
            "schema_version": "1.0",
            "pair_id": pair_id,
            "prospect_id": prospect_id,
            "repetition": repetition,
            "outputs": [
                _review_output("A", artifacts[(prospect_id, repetition, labels["A"])]),
                _review_output("B", artifacts[(prospect_id, repetition, labels["B"])]),
            ],
            "review": {
                "prospect_id": prospect_id,
                "repetition": repetition,
                "outputs": [
                    {
                        "label": label,
                        "claim_reviews": [
                            {
                                "finding_id": finding.finding_id,
                                "supported": None,
                                "reference_fact_ids": [],
                                "source_quality": None,
                                "critical_error": None,
                                "novel_useful": False,
                            }
                            for finding in artifacts[(prospect_id, repetition, labels[label])].evidence.findings
                        ],
                        "covered_areas": [],
                    }
                    for label in ("A", "B")
                ],
                "preferred": None,
                "reason": "",
            },
        }
        write_json(review_dir / f"{pair_id}.json", packet)

    mapping_path = private_dir / "blind-map.json"
    write_json(mapping_path, {"schema_version": "1.0", "pairs": mapping})
    return review_dir, mapping_path
