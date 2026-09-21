from __future__ import annotations

import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

from .config import CONFIG_DIR, read_json, write_json
from .models import PairReview, RunArtifact


def _write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# Firecrawl–Exa comparison report",
        "",
        f"**Decision:** `{report['decision']}`",
        "",
        report["reason"],
        "",
        "## Evidence quality",
        "",
        "| Provider | Safety gate | Precision | Reference coverage | Critical errors |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for provider in ("firecrawl", "exa"):
        item = report["evidence_metrics"][provider]
        lines.append(
            f"| {provider.title()} | {'Pass' if item['safety_gate_pass'] else 'Fail'} | "
            f"{item['precision']:.1%} | {item['reference_coverage_macro']:.1%} | "
            f"{len(item['critical_errors'])} |"
        )
    lines.extend(
        [
            "",
            "## Operations",
            "",
            "| Provider | Completed | Within 30 s | Median latency | Usable pages |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for provider in ("firecrawl", "exa"):
        item = report["operational_metrics"][provider]
        median = item["median_duration_ms"]
        lines.append(
            f"| {provider.title()} | {item['completion_rate']:.1%} | "
            f"{item['within_30_seconds_rate']:.1%} | "
            f"{median if median is not None else 'n/a'} ms | {item['usable_page_rate']:.1%} |"
        )
    lines.extend(["", "## Blinded preference", ""])
    for prospect_id, preference in report["per_prospect_blinded_preference"].items():
        lines.append(f"- {prospect_id}: {preference}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _empty_metrics() -> dict[str, Any]:
    return {
        "claims": 0,
        "supported_claims": 0,
        "critical_errors": [],
        "reference_facts": set(),
        "novel_useful": 0,
        "source_quality": defaultdict(int),
        "covered_areas": set(),
        "reviewed_prospects": set(),
        "by_prospect": defaultdict(lambda: {"claims": 0, "supported": 0, "reference_facts": set()}),
    }


def _finalize_metrics(metrics: dict[str, Any], eligible: dict[str, Any]) -> dict[str, Any]:
    by_prospect: dict[str, Any] = {}
    coverage_values = []
    for prospect_id in eligible:
        values = metrics["by_prospect"][prospect_id]
        denominator = {
            fact_id for fact_ids in eligible.get(prospect_id, {}).values() for fact_id in fact_ids
        }
        recovered = values["reference_facts"] & denominator
        coverage = len(recovered) / len(denominator) if denominator else None
        if coverage is not None:
            coverage_values.append(coverage)
        by_prospect[prospect_id] = {
            "precision": values["supported"] / values["claims"] if values["claims"] else 1.0,
            "reference_coverage": coverage,
            "recovered_reference_facts": sorted(recovered),
        }
    precision = metrics["supported_claims"] / metrics["claims"] if metrics["claims"] else 0.0
    per_case_precision = [value["precision"] for value in by_prospect.values()]
    safety_pass = (
        not metrics["critical_errors"]
        and precision >= 0.90
        and metrics["reviewed_prospects"] == set(eligible)
        and all(value >= 0.80 for value in per_case_precision)
    )
    return {
        "precision": precision,
        "reference_coverage_macro": statistics.mean(coverage_values) if coverage_values else 0.0,
        "critical_errors": metrics["critical_errors"],
        "safety_gate_pass": safety_pass,
        "novel_useful_findings": metrics["novel_useful"],
        "source_quality": dict(metrics["source_quality"]),
        "covered_areas": sorted(metrics["covered_areas"]),
        "by_prospect": by_prospect,
    }


def _operational_metrics(run_dir: Path, provider: str) -> dict[str, Any]:
    artifacts = [
        RunArtifact.model_validate(read_json(path))
        for path in (run_dir / provider).glob("*/run-*.json")
    ]
    durations = [item.total_duration_ms for item in artifacts]
    usable = sum(page.usable for item in artifacts for page in item.pages)
    pages = sum(len(item.pages) for item in artifacts)
    return {
        "runs": len(artifacts),
        "completion_rate": (
            sum(item.status == "completed" for item in artifacts) / len(artifacts) if artifacts else 0.0
        ),
        "within_30_seconds_rate": (
            sum(item.total_duration_ms <= 30000 for item in artifacts) / len(artifacts) if artifacts else 0.0
        ),
        "median_duration_ms": statistics.median(durations) if durations else None,
        "slowest_duration_ms": max(durations) if durations else None,
        "usable_page_rate": usable / pages if pages else 0.0,
        "search_calls": sum(call.operation == "search" for item in artifacts for call in item.calls),
        "retrieval_calls": sum(call.operation == "retrieve" for item in artifacts for call in item.calls),
        "failed_calls": sum(not call.success for item in artifacts for call in item.calls),
        "raw_usage": [call.usage for item in artifacts for call in item.calls if call.usage],
    }


def score(run_dir: Path, review_dir: Path | None = None) -> dict[str, Any]:
    review_dir = review_dir or run_dir / "review"
    mapping = read_json(run_dir / "_private" / "blind-map.json")["pairs"]
    eligible = read_json(CONFIG_DIR / "eligible-reference-facts.json")
    metrics = {"firecrawl": _empty_metrics(), "exa": _empty_metrics()}
    pair_preferences: list[dict[str, Any]] = []

    for path in sorted(review_dir.glob("*.json")):
        packet = read_json(path)
        review = PairReview.model_validate(packet["review"])
        labels = mapping[packet["pair_id"]]
        for output in review.outputs:
            provider = labels[output.label]
            target = metrics[provider]
            target["reviewed_prospects"].add(review.prospect_id)
            target["covered_areas"].update(area.value for area in output.covered_areas)
            for claim in output.claim_reviews:
                target["claims"] += 1
                target["supported_claims"] += int(claim.supported)
                target["reference_facts"].update(claim.reference_fact_ids)
                target["novel_useful"] += int(claim.novel_useful)
                target["source_quality"][claim.source_quality] += 1
                case = target["by_prospect"][review.prospect_id]
                case["claims"] += 1
                case["supported"] += int(claim.supported)
                case["reference_facts"].update(claim.reference_fact_ids)
                if claim.critical_error:
                    target["critical_errors"].append(
                        {"prospect_id": review.prospect_id, "finding_id": claim.finding_id, "error": claim.critical_error}
                    )
        preferred_provider = labels.get(review.preferred) if review.preferred != "tie" else "tie"
        pair_preferences.append(
            {"prospect_id": review.prospect_id, "repetition": review.repetition, "preferred": preferred_provider}
        )

    final = {provider: _finalize_metrics(values, eligible) for provider, values in metrics.items()}
    per_prospect_preferences: dict[str, str] = {}
    for prospect_id in sorted({item["prospect_id"] for item in pair_preferences}):
        choices = [item["preferred"] for item in pair_preferences if item["prospect_id"] == prospect_id]
        fc, exa = choices.count("firecrawl"), choices.count("exa")
        per_prospect_preferences[prospect_id] = "firecrawl" if fc > exa else "exa" if exa > fc else "tie"

    passes = [provider for provider in ("firecrawl", "exa") if final[provider]["safety_gate_pass"]]
    decision, reason = "select_neither", "Neither provider passed the safety gate."
    if len(passes) == 1:
        decision, reason = passes[0], "Only this provider passed the safety gate."
    elif len(passes) == 2:
        utility = {
            provider: sum(value == provider for value in per_prospect_preferences.values())
            for provider in passes
        }
        losses = {provider: utility[[p for p in passes if p != provider][0]] for provider in passes}
        material = [provider for provider in passes if utility[provider] >= 3 and losses[provider] <= 1]
        coverage_diff = final["firecrawl"]["reference_coverage_macro"] - final["exa"]["reference_coverage_macro"]
        precision_diff = final["firecrawl"]["precision"] - final["exa"]["precision"]
        if len(material) == 1:
            decision, reason = material[0], "Material blinded downstream-utility advantage."
        elif abs(coverage_diff) >= 0.10 and abs(precision_diff) <= 0.02:
            decision = "firecrawl" if coverage_diff > 0 else "exa"
            reason = "Material macro-average reference-coverage advantage."
        else:
            decision = "tie_requires_cost_inputs"
            reason = "No material quality winner; compare effective cost, then latency, using the dated billing records."

    report = {
        "schema_version": "1.0",
        "decision": decision,
        "reason": reason,
        "evidence_metrics": final,
        "operational_metrics": {
            provider: _operational_metrics(run_dir, provider) for provider in ("firecrawl", "exa")
        },
        "per_prospect_blinded_preference": per_prospect_preferences,
        "pair_preferences": pair_preferences,
    }
    write_json(run_dir / "comparison-report.json", report)
    _write_markdown(run_dir / "comparison-report.md", report)
    return report
