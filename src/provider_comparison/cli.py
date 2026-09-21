from __future__ import annotations

import argparse
import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

from .blinding import create_blind_packets
from .config import ROOT, load_project_env, load_prospects, load_settings, read_json, write_json
from .extractor import GeminiExtractor, NoopExtractor
from .drafter import GeminiDrafter, build_draft_input
from .models import QueryPlan, RunArtifact
from .pipeline import run_prospect, save_artifact
from .queries import build_plan
from .scoring import score
from .search_only_experiment import render_markdown, run_search_only_experiment
from .identity import IdentityLookup, lookup_prospect, confirm_match, confirmed_prospect, presentation, research_confirmed
from .models import Prospect


def _default_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def command_plan(args: argparse.Namespace) -> None:
    plan = build_plan(load_prospects())
    output = Path(args.output) if args.output else ROOT / "comparison" / "artifacts" / "plan.json"
    write_json(output, plan)
    print(output)


async def _run(args: argparse.Namespace) -> None:
    load_project_env()
    settings = load_settings()
    plan = (
        QueryPlan.model_validate(read_json(Path(args.plan)))
        if args.plan
        else build_plan(load_prospects())
    )
    selected_ids = set(args.prospect or [prospect.id for prospect in plan.prospects])
    prospects = [prospect for prospect in plan.prospects if prospect.id in selected_ids]
    if not prospects:
        raise SystemExit("No matching prospects selected.")
    providers = ["exa"]
    required_keys = []
    if "firecrawl" in providers:
        required_keys.append("FIRECRAWL_API_KEY")
    if "exa" in providers:
        required_keys.append("EXA_API_KEY")
    if not args.skip_extraction:
        required_keys.append("GEMINI_API_KEY")
    missing_keys = [key for key in required_keys if not os.environ.get(key)]
    if missing_keys:
        raise SystemExit("Missing required environment variables: " + ", ".join(missing_keys))
    repetitions = args.repetitions or settings.repetitions
    run_id = args.run_id or _default_run_id()
    run_dir = ROOT / "comparison" / "artifacts" / run_id
    write_json(run_dir / "plan.json", plan)

    extractor = NoopExtractor() if args.skip_extraction else GeminiExtractor(settings.gemini_model)
    for prospect_index, prospect in enumerate(prospects):
        for repetition in range(1, repetitions + 1):
            ordered = providers if (prospect_index + repetition) % 2 else list(reversed(providers))
            for provider_name in ordered:
                existing_path = run_dir / provider_name / prospect.id / f"run-{repetition:02d}.json"
                if existing_path.exists() and not args.refresh:
                    RunArtifact.model_validate(read_json(existing_path))
                    print(f"reused\t{provider_name}\t{prospect.id}\t{repetition}\t{existing_path}")
                    continue
                artifact = await run_prospect(
                    run_id=run_id,
                    repetition=repetition,
                    provider_name=provider_name,
                    prospect=prospect,
                    queries=plan.queries[prospect.id],
                    settings=settings,
                    extractor=extractor,
                )
                path = save_artifact(run_dir, artifact)
                print(f"{artifact.status}\t{provider_name}\t{prospect.id}\t{repetition}\t{path}")
    print(f"run_dir={run_dir}")


def command_validate(_: argparse.Namespace) -> None:
    settings = load_settings()
    prospects = load_prospects()
    plan = build_plan(prospects)
    assert all(len(queries) == 3 for queries in plan.queries.values())
    assert settings.max_search_calls <= 6
    assert settings.max_pages <= 8
    print(f"valid: {len(prospects)} prospects, {sum(map(len, plan.queries.values()))} fixed queries")


def command_preflight(_: argparse.Namespace) -> None:
    load_project_env()
    command_validate(argparse.Namespace())
    required = ("EXA_API_KEY", "GEMINI_API_KEY")
    missing = [key for key in required if not os.environ.get(key)]
    for key in required:
        print(f"{key}: {'present' if os.environ.get(key) else 'missing'}")
    if missing:
        raise SystemExit("Missing required environment variables: " + ", ".join(missing))
    print("preflight passed; no network calls were made")


def command_blind(args: argparse.Namespace) -> None:
    review_dir, mapping_path = create_blind_packets(Path(args.run_dir), args.seed)
    print(f"review_dir={review_dir}")
    print(f"private_mapping={mapping_path}")


def command_score(args: argparse.Namespace) -> None:
    report = score(Path(args.run_dir), Path(args.review_dir) if args.review_dir else None)
    print(f"decision={report['decision']}")
    print(f"report={Path(args.run_dir) / 'comparison-report.json'}")


async def _search_only(args: argparse.Namespace) -> None:
    load_project_env()
    missing = [key for key in ("FIRECRAWL_API_KEY", "EXA_API_KEY") if not os.environ.get(key)]
    if missing:
        raise SystemExit("Missing required environment variables: " + ", ".join(missing))
    selected = set(args.prospect or ["P01", "P02", "P03"])
    prospects = [prospect for prospect in load_prospects() if prospect.id in selected]
    if not prospects:
        raise SystemExit("No matching prospects selected.")
    artifact = await run_search_only_experiment(
        prospects=prospects,
        settings=load_settings(),
        limit=args.results,
        query_style=args.query_style,
    )
    run_id = args.run_id or _default_run_id()
    run_dir = ROOT / "comparison" / "artifacts" / run_id
    json_path = run_dir / "search-only-results.json"
    report_path = run_dir / "search-only-report.md"
    write_json(json_path, artifact)
    report_path.write_text(render_markdown(artifact), encoding="utf-8")
    print(f"json={json_path}")
    print(f"report={report_path}")


async def _draft(args: argparse.Namespace) -> None:
    source = Path(args.artifact).resolve()
    output = Path(args.output).resolve() if args.output else source.with_suffix(".draft.json")
    if output.exists():
        raise SystemExit("Draft output already exists; choose a new --output path.")
    artifact = RunArtifact.model_validate(read_json(source))
    identity = artifact.shared_enrichment.get("identity")
    if identity:
        matched = confirmed_prospect(IdentityLookup.model_validate(identity))
        if matched != artifact.prospect:
            raise ValueError("Research prospect differs from the user-confirmed identity")
        if args.verified_role and args.verified_role != matched.role:
            raise ValueError("Role differs from the user-confirmed identity")
        args.verified_role = matched.role
    elif not args.verified_role:
        raise ValueError("Legacy research requires --verified-role; use research-confirmed for the user-confirmed workflow")
    # Check local prerequisites before constructing the model client or making a paid call.
    build_draft_input(artifact, args.verified_role, datetime.now(UTC).date())
    load_project_env()
    settings = load_settings()
    drafter = GeminiDrafter(
        settings.gemini_model,
        settings.draft_timeout_seconds,
        temperature=settings.draft_temperature,
    )
    try:
        result = await drafter.draft(
            artifact, research_artifact=str(source), verified_role=args.verified_role
        )
    finally:
        await drafter.close()
    write_json(output, result)
    print(f"draft={output}")
    if not result.call.success:
        raise SystemExit(result.call.error)


def _new_output(path: str) -> Path:
    output = Path(path).resolve()
    if output.exists():
        raise SystemExit("Output already exists; choose a new path.")
    return output


async def _identify(args: argparse.Namespace) -> None:
    output = _new_output(args.output)
    prospect = Prospect(id=args.id, name=args.name, company=args.company,
                        role=args.role, profile_url=args.profile_url)
    load_project_env()
    lookup = await lookup_prospect(prospect, load_settings())
    write_json(output, lookup)
    import json
    print(json.dumps(presentation(lookup), ensure_ascii=False, indent=2))
    print(f"lookup={output}")
    if any(not call.success for call in lookup.calls):
        raise SystemExit("Lookup failed; inspect the saved call records.")


def _confirm_identity(args: argparse.Namespace) -> None:
    output = _new_output(args.output)
    lookup = IdentityLookup.model_validate(read_json(Path(args.lookup)))
    confirmed = confirm_match(lookup, args.candidate, confirmed_by=args.user)
    write_json(output, confirmed)
    print(f"Confirmed by you: {confirmed_prospect(confirmed).name}\nconfirmation={output}")


async def _research_confirmed(args: argparse.Namespace) -> None:
    output = _new_output(args.output)
    lookup = IdentityLookup.model_validate(read_json(Path(args.identity)))
    confirmed_prospect(lookup)
    load_project_env()
    artifact = await research_confirmed(lookup, load_settings(), run_id=args.run_id or _default_run_id())
    write_json(output, artifact)
    print(f"research={output}")
    if artifact.status != "completed":
        raise SystemExit(f"Research {artifact.status}; inspect saved call records")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Firecrawl versus Exa comparison harness")
    commands = parser.add_subparsers(dest="command", required=True)

    identify = commands.add_parser("identify", help="Find prospect matches with Firecrawl for user review")
    for field in ("id", "name", "company", "role", "output"):
        identify.add_argument(f"--{field}", required=True)
    identify.add_argument("--profile-url")
    identify.set_defaults(async_func=_identify)

    confirm = commands.add_parser("confirm-identity", help="Record the user's explicit selection; no network calls")
    confirm.add_argument("--lookup", required=True)
    confirm.add_argument("--candidate", type=int, required=True, help="Zero-based candidate index shown in lookup")
    confirm.add_argument("--user", required=True, help="Identity of the user taking this action")
    confirm.add_argument("--output", required=True)
    confirm.set_defaults(func=_confirm_identity)

    research = commands.add_parser("research-confirmed", help="Run Exa research only after user confirmation")
    research.add_argument("--identity", required=True)
    research.add_argument("--output", required=True)
    research.add_argument("--run-id")
    research.set_defaults(async_func=_research_confirmed)

    validate_parser = commands.add_parser("validate", help="Validate config and query budgets")
    validate_parser.set_defaults(func=command_validate)

    preflight_parser = commands.add_parser("preflight", help="Check config and secret presence without network calls")
    preflight_parser.set_defaults(func=command_preflight)

    plan_parser = commands.add_parser("plan", help="Freeze and write the shared query plan")
    plan_parser.add_argument("--output")
    plan_parser.set_defaults(func=command_plan)

    run_parser = commands.add_parser("run", help="Run the locked Exa research pipeline")
    run_parser.add_argument("--provider", choices=["exa"], default="exa")
    run_parser.add_argument("--prospect", action="append", help="Prospect ID; repeat to select several")
    run_parser.add_argument("--repetitions", type=int, choices=[1, 2, 3])
    run_parser.add_argument("--run-id")
    run_parser.add_argument("--plan")
    run_parser.add_argument("--skip-extraction", action="store_true")
    run_parser.add_argument(
        "--refresh", action="store_true",
        help="Replace existing artifacts and repeat Exa calls for the same run ID",
    )
    run_parser.set_defaults(async_func=_run)

    draft_parser = commands.add_parser("draft", help="Draft from saved search-only research (one Gemini call)")
    draft_parser.add_argument("--artifact", required=True, help="Existing RunArtifact JSON; no new searches")
    draft_parser.add_argument(
        "--verified-role",
        help="Legacy harness only; confirmed research supplies its role automatically",
    )
    draft_parser.add_argument("--output", help="New output path; defaults to <artifact stem>.draft.json")
    draft_parser.set_defaults(async_func=_draft)

    blind_parser = commands.add_parser("blind", help="Create provider-neutral review packets")
    blind_parser.add_argument("--run-dir", required=True)
    blind_parser.add_argument("--seed", default="zamp-v1")
    blind_parser.set_defaults(func=command_blind)

    score_parser = commands.add_parser("score", help="Score completed blind reviews")
    score_parser.add_argument("--run-dir", required=True)
    score_parser.add_argument("--review-dir")
    score_parser.set_defaults(func=command_score)

    search_only_parser = commands.add_parser(
        "search-only", help="Test whether provider search highlights are sufficient without page retrieval"
    )
    search_only_parser.add_argument("--prospect", action="append", help="Prospect ID; repeat to select several")
    search_only_parser.add_argument("--results", type=int, choices=[3, 4, 5], default=5)
    search_only_parser.add_argument("--query-style", choices=["baseline", "evidence"], default="baseline")
    search_only_parser.add_argument("--run-id")
    search_only_parser.set_defaults(async_func=_search_only)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if hasattr(args, "async_func"):
        asyncio.run(args.async_func(args))
    else:
        args.func(args)


if __name__ == "__main__":
    main()
