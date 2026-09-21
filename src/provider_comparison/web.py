"""Local, single-user outreach workspace. Run: python -m provider_comparison.web."""
from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import json
import ipaddress
import os
import re
import shutil
import socket
import secrets
import threading
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .config import ROOT, load_project_env, load_settings, read_json, write_json
from .drafter import GeminiDrafter, build_draft_input
from .identity import IdentityLookup, confirm_match, confirmed_prospect, lookup_prospect, presentation, research_confirmed
from .models import Prospect, RunArtifact, SearchResult
from .providers import ExaProvider
from .selection import canonical_url

STATIC = Path(__file__).with_name("static")
AREAS = ["professional_profile", "company_developments", "recruiting_context"]
DEMO_PROSPECTS = ROOT / "comparison" / "config" / "demo-prospects.json"
PRODUCT_BRIEF = ROOT / "docs" / "product-brief.md"


def now():
    return datetime.now(UTC).isoformat()


def identity_error(lookup):
    failed = next((call for call in lookup.calls if not call.success), None)
    if failed is None:
        return "Identity lookup could not finish. Retry to check this prospect."
    service = "Firecrawl search" if failed.operation == "search" else "the identity model"
    if (failed.error or "").startswith("Identity response failed evidence validation"):
        return "The identity model returned a match with evidence that did not pass validation. Confirmation is blocked; retry the lookup to generate a new match."
    if failed.status_code in (401, 403):
        return f"Access to {service} was denied. Check the configured API key and permissions."
    if failed.status_code == 429:
        return f"{service.capitalize()} reached a rate or usage limit. Check the account before retrying."
    if failed.operation == "search" and failed.status_code is None:
        return "Could not connect to Firecrawl search. Check this server’s network access, then retry. No identity assessment was made."
    return f"The request to {service} could not finish. Retry this stage."


class WorkspaceServer(ThreadingHTTPServer):
    # Windows SO_REUSEADDR can silently allow two servers on one port.
    allow_reuse_address = False

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Workspace:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.runs = {}
        self.settings = load_settings()
        for path in directory.glob("*/state.json"):
            try:
                run = read_json(path)
                if run["status"] == "running":
                    run.update(status="failed", error="The server stopped during this stage. Retry to continue.")
                self.runs[run["id"]] = run
            except (ValueError, KeyError):
                continue

    def save(self, run):
        run["updated_at"] = now()
        path = self.directory / run["id"] / "state.json"
        temporary = path.with_suffix(".tmp")
        write_json(temporary, run)
        temporary.replace(path)

    def update(self, run_id, **values):
        with self.lock:
            run = self.runs[run_id]
            run.update(values)
            self.save(run)

    def get(self, run_id):
        with self.lock:
            if run_id not in self.runs:
                raise ValueError("Run not found")
            result = copy.deepcopy(self.runs[run_id])
            if result.get("research"):
                artifact = RunArtifact.model_validate(read_json(self.directory / run_id / "research.json"))
                result["available_sources"] = build_draft_input(
                    artifact, result["prospect"]["role"], datetime.now(UTC).date(),
                    additional_sources=result.get("added_sources"),
                )["research_bundle"]["source_passages"]
            return result

    def listing(self):
        with self.lock:
            return [dict(id=r["id"], prospect=r["prospect"], stage=r["stage"], status=r["status"],
                         draft_confirmed_at=r.get("draft_confirmed_at"),
                         created_at=r["created_at"], updated_at=r["updated_at"])
                    for r in sorted(self.runs.values(), key=lambda r: r["created_at"], reverse=True)]

    def create(self, data):
        name, company = str(data.get("name", "")).strip(), str(data.get("company", "")).strip()
        role = str(data.get("role", "")).strip()
        profile = str(data.get("profile_url", "")).strip()
        if not name or not company or max(len(name), len(company)) > 160:
            raise ValueError("Enter a name and company (up to 160 characters each).")
        if not role or len(role) > 200:
            raise ValueError("Enter the prospect’s role (up to 200 characters).")
        if profile and (urlparse(profile).scheme not in {"http", "https"} or not urlparse(profile).hostname or len(profile) > 2000):
            raise ValueError("Enter a valid profile URL starting with https://.")
        run_id = uuid.uuid4().hex
        prospect = Prospect(id=run_id, name=name, company=company, role=role, profile_url=profile or None)
        run = dict(id=run_id, created_at=now(), updated_at=now(), prospect=prospect.model_dump(),
                   stage="identify", status="running", stage_started_at=now(), identity=None,
                   areas={}, research=None, output=None, edits=None, error=None, attempt=0,
                   draft_confirmed_at=None)
        with self.lock:
            self.runs[run_id] = run
            self.save(run)
        self.launch(run_id, "identify")
        return self.get(run_id)

    def launch(self, run_id, stage):
        def work():
            try:
                asyncio.run(self.execute(run_id, stage))
            except Exception:
                self.update(run_id, status="failed", error="This stage could not finish. Check the API configuration and retry.")
        threading.Thread(target=work, daemon=True).start()

    def action(self, run_id, action, data):
        with self.lock:
            run = self.runs[run_id]
            if action == "save":
                if run["stage"] != "review" or not (run.get("output") or {}).get("draft"):
                    raise ValueError("No draft is available to edit.")
                if data.get("draft_version") != run["attempt"]:
                    raise ValueError("This draft has changed. Reopen the run before editing.")
                subject, body = str(data.get("subject", "")), str(data.get("body", ""))
                if not subject.strip() or not body.strip() or len(subject) > 500 or len(body) > 20000 or "\n" in subject or "\r" in subject:
                    raise ValueError("Enter a subject and message within the editor limits.")
                run["edits"] = dict(subject=subject, body=body)
                self.save(run)
                return self.get(run_id)
            if run["status"] == "running":
                raise ValueError("This run is already in progress.")
            if action == "confirm":
                if run["stage"] != "identify" or run["status"] != "needs_input":
                    raise ValueError("This identity is not awaiting confirmation.")
                lookup = IdentityLookup.model_validate(read_json(self.directory / run_id / "lookup.json"))
                if data.get("lookup_id") != lookup.lookup_id:
                    raise ValueError("The identity lookup changed. Refresh before confirming.")
                lookup = confirm_match(lookup, int(data["candidate"]), confirmed_by="local-workspace-user")
                write_json(self.directory / run_id / "confirmed.json", lookup)
                run["identity"] = presentation(lookup)
                run["prospect"] = confirmed_prospect(lookup).model_dump(mode="json")
                stage = "research"
            elif action == "retry":
                if run["status"] != "failed":
                    raise ValueError("Only a failed stage can be retried.")
                stage = run["stage"]
            elif action == "research":
                if run["stage"] != "review":
                    raise ValueError("Research can be restarted from Review.")
                stage = "research"
            elif action == "regenerate":
                if run["stage"] != "review" or run["status"] != "ready":
                    raise ValueError("Regeneration is available after reviewing a draft.")
                if data.get("draft_version") != run["attempt"]:
                    raise ValueError("This draft has changed. Reopen the run before regenerating.")
                comments = data.get("comments", "")
                if not isinstance(comments, str) or len(comments) > 4000:
                    raise ValueError("Keep regeneration instructions within 4,000 characters.")
                selected = data.get("selected_source_ids")
                if selected is not None:
                    artifact = RunArtifact.model_validate(read_json(self.directory / run_id / "research.json"))
                    build_draft_input(artifact, run["prospect"]["role"], datetime.now(UTC).date(), selected,
                                      additional_sources=run.get("added_sources"))
                previous = run.get("edits") or (run.get("output") or {}).get("draft")
                write_json(self.directory / run_id / f"review-{run['attempt']}.json",
                           {"output": run["output"], "edits": run.get("edits")})
                run["regeneration"] = {
                    "reason": "The previous draft was not satisfactory to the user.",
                    "previous_draft": {k: previous[k] for k in ("subject", "body")} if previous else None,
                    "user_comments": comments.strip(),
                    "selected_source_ids": selected,
                }
                run["draft_confirmed_at"] = None
                stage = "draft"
            elif action == "approve":
                if run["stage"] != "review" or run["status"] != "ready" or not (run.get("output") or {}).get("draft"):
                    raise ValueError("A completed draft is required before it can be confirmed.")
                run["draft_confirmed_at"] = now()
                self.save(run)
                return self.get(run_id)
            else:
                raise ValueError("Unknown action")
            run.update(stage=stage, status="running", stage_started_at=now(), error=None)
            if stage == "research":
                if run.get("output"):
                    write_json(self.directory / run_id / f"review-{run['attempt']}.json",
                               {"output": run["output"], "edits": run.get("edits")})
                run.update(areas={}, research=None, output=None, edits=None, regeneration=None, added_sources=[])
            self.save(run)
        self.launch(run_id, stage)
        return self.get(run_id)

    def delete(self, run_id):
        with self.lock:
            if run_id not in self.runs:
                raise ValueError("Run not found")
            folder = (self.directory / run_id).resolve()
            root = self.directory.resolve()
            if folder.parent != root:
                raise ValueError("Invalid run folder")
            del self.runs[run_id]
            if folder.exists():
                shutil.rmtree(folder)

    async def add_source(self, run_id, data):
        run = self.get(run_id)
        if run["stage"] != "review" or run["status"] != "ready" or data.get("draft_version") != run["attempt"]:
            raise ValueError("Reopen the completed draft before adding a source.")
        url = data.get("url")
        if not isinstance(url, str) or len(url) > 2000:
            raise ValueError("Enter a public https:// page URL.")
        parsed = urlparse(url.strip())
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or not host or "." not in host or parsed.username or parsed.password
                or parsed.port not in (None, 443) or host.endswith((".localhost", ".local", ".internal"))):
            raise ValueError("Enter a public https:// page URL without login details.")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None:
            raise ValueError("Use a public website address, not an IP address.")
        if any(canonical_url(s["url"]) == canonical_url(url) for s in run.get("available_sources", [])):
            raise ValueError("This URL is already available in the source list.")
        if len(run.get("added_sources", [])) >= 3:
            raise ValueError("You can add up to three URLs per research run.")
        source_id = "ADDED-" + uuid.uuid4().hex[:12]
        provider = ExaProvider(os.environ.get("EXA_API_KEY", ""), self.settings.exa_base_url,
                               self.settings.request_timeout_seconds)
        try:
            page = await provider.retrieve(SearchResult(area="recruiting_context", query_id=source_id,
                                          rank=1, url=url.strip()), source_id, 6000)
        finally:
            await provider.close()
        if not page.usable or not page.content.strip():
            raise ValueError("Could not read this page. Try a public article or another URL; your draft is unchanged.")
        source = dict(source_id=source_id, url=page.url, title=page.title, passage=page.content,
                      published_at=page.published_at, collected_at=page.retrieved_at.isoformat(),
                      evidence_kind="retrieved_page")
        with self.lock:
            current = self.runs[run_id]
            if current["attempt"] != run["attempt"] or current["status"] != "ready" or current["stage"] != "review":
                raise ValueError("The draft changed while this URL was loading. Reopen it and try again.")
            if len(current.get("added_sources", [])) >= 3:
                raise ValueError("You can add up to three URLs per research run.")
            if any(canonical_url(s["url"]) == canonical_url(url) for s in current.get("added_sources", [])):
                raise ValueError("This URL is already available in the source list.")
            write_json(self.directory / run_id / f"{source_id}.json", {"page": page.model_dump(mode="json"),
                       "calls": [call.model_dump(mode="json") for call in provider.calls]})
            current.setdefault("added_sources", []).append(source)
            self.save(current)
        return self.get(run_id)

    async def execute(self, run_id, stage):
        run = self.get(run_id)
        folder = self.directory / run_id
        attempt = run["attempt"] + 1
        self.update(run_id, attempt=attempt)
        if stage == "identify":
            lookup = await lookup_prospect(Prospect.model_validate(run["prospect"]), self.settings)
            write_json(folder / f"lookup-{attempt}.json", lookup)
            write_json(folder / "lookup.json", lookup)
            healthy = lookup.result is not None and all(c.success for c in lookup.calls)
            self.update(run_id, identity=presentation(lookup), status="needs_input" if healthy else "failed",
                        error=None if healthy else identity_error(lookup))
            return  # A human confirmation is always required before research.
        lookup = IdentityLookup.model_validate(read_json(folder / "confirmed.json"))
        prospect = confirmed_prospect(lookup)
        if stage == "research":
            self.update(run_id, areas={area: dict(status="searching", results=[]) for area in AREAS})

            def progress(query, results, calls):
                with self.lock:
                    current = self.runs[run_id]
                    current["areas"][query.area.value] = dict(
                        status="complete" if calls and all(c.success for c in calls) else "failed",
                        collected_at=now(), results=[r.model_dump(mode="json") for r in results])
                    self.save(current)

            artifact = await research_confirmed(lookup, self.settings, run_id=run_id, on_result=progress)
            write_json(folder / f"research-{attempt}.json", artifact)
            write_json(folder / "research.json", artifact)
            research = dict(status=artifact.status, pages=[p.model_dump(mode="json") for p in artifact.pages],
                            collected_at=artifact.completed_at.isoformat())
            self.update(run_id, research=research)
            with self.lock:
                for area in self.runs[run_id]["areas"].values():
                    if area["status"] == "searching":
                        area["status"] = "failed"
                self.save(self.runs[run_id])
            if artifact.status in ("failed", "timed_out") and not any(p.usable for p in artifact.pages if not p.page_id.startswith("IDENTITY-")):
                self.update(run_id, status="failed", error="Research returned no usable passages. Retry research or edit the prospect.")
                return
            self.update(run_id, stage="draft", stage_started_at=now())
        artifact = RunArtifact.model_validate(read_json(folder / "research.json"))
        drafter = GeminiDrafter(self.settings.gemini_model, self.settings.draft_timeout_seconds,
                                temperature=self.settings.draft_temperature)
        try:
            result = await drafter.draft(artifact, research_artifact=str(folder / "research.json"),
                                         verified_role=prospect.role, with_review=True,
                                         regeneration=run.get("regeneration"),
                                         additional_sources=run.get("added_sources"))
        finally:
            await drafter.close()
        write_json(folder / f"draft-{attempt}.json", result)
        if not result.call.success:
            message = "Draft generation could not finish. Retry drafting using the saved research."
            if (result.call.error or "").startswith("Draft response failed"):
                message = "The generated response did not pass the draft format or evidence checks. Retry drafting; your research is saved."
            elif "timed out" in (result.call.error or ""):
                message = "Draft generation timed out. Retry drafting; your research is saved."
            self.update(run_id, status="failed", error=message)
            return
        used = set(result.draft.source_ids if result.draft else [])
        output = dict(draft=result.draft.model_dump() if result.draft else None,
                      review=result.review.model_dump(), strength=result.review.strength(),
                      sources=[s for s in result.input["research_bundle"]["source_passages"] if s["source_id"] in used])
        self.update(run_id, stage="review", status="ready", output=output, edits=None)


class Handler(BaseHTTPRequestHandler):
    workspace: Workspace
    public_origin: str | None = None
    demo_password: str | None = None

    def log_message(self, *_):
        pass

    def reply(self, status, value, content_type="application/json", headers=None):
        payload = json.dumps(value, ensure_ascii=False).encode() if content_type == "application/json" else value
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(payload)

    def trusted(self):
        host = self.headers.get("Host", "")
        allowed = {f"127.0.0.1:{self.server.server_port}": f"http://127.0.0.1:{self.server.server_port}",
                   f"localhost:{self.server.server_port}": f"http://localhost:{self.server.server_port}"}
        if self.public_origin:
            allowed[urlparse(self.public_origin).netloc] = self.public_origin
        origin = self.headers.get("Origin")
        return host in allowed and (not origin or origin == allowed[host])

    def authorized(self):
        if not self.trusted():
            self.reply(403, {"error": "This request origin is not allowed."})
            return False
        if self.public_origin:
            expected = "Basic " + base64.b64encode(f"demo:{self.demo_password}".encode()).decode()
            if not self.demo_password or not secrets.compare_digest(
                    self.headers.get("Authorization", "").encode(), expected.encode()):
                self.reply(401, {"error": "Enter the demo credentials to continue."},
                           headers={"WWW-Authenticate": 'Basic realm="InterviewPath demo", charset="UTF-8"'})
                return False
        return True

    def do_GET(self):
        if not self.authorized():
            return
        path = urlparse(self.path).path
        if path == "/api/runs":
            return self.reply(200, self.workspace.listing())
        if path == "/api/config":
            return self.reply(200, {"missing": [k for k in ("FIRECRAWL_API_KEY", "EXA_API_KEY", "GEMINI_API_KEY") if not os.environ.get(k)]})
        if path == "/api/demo":
            prospects = read_json(DEMO_PROSPECTS)
            brief = PRODUCT_BRIEF.read_text(encoding="utf-8").split("## Outreach objective", 1)[0]
            brief = brief.replace("Zamp is the company whose hiring assessment this workspace supports. The product described below is the product from the case study; it is not Zamp's own product.\n\n", "")
            return self.reply(200, {"prospects": prospects, "product_brief": brief})
        if re.fullmatch(r"/api/runs/[a-f0-9]{32}", path):
            try:
                return self.reply(200, self.workspace.get(path.split("/")[-1]))
            except ValueError:
                return self.reply(404, {"error": "Run not found"})
        assets = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8")}
        if path in assets:
            filename, mime = assets[path]
            return self.reply(200, (STATIC / filename).read_bytes(), mime)
        self.reply(404, {"error": "Not found"})

    def do_POST(self):
        if not self.authorized():
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536 or not self.headers.get("Content-Type", "").startswith("application/json"):
                raise ValueError("Expected a JSON request within the size limit.")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Expected an object")
            if self.path == "/api/runs":
                return self.reply(201, self.workspace.create(data))
            match = re.fullmatch(r"/api/runs/([a-f0-9]{32})/sources", self.path)
            if match:
                return self.reply(200, asyncio.run(self.workspace.add_source(match[1], data)))
            match = re.fullmatch(r"/api/runs/([a-f0-9]{32})/(confirm|retry|research|save|regenerate|approve)", self.path)
            if match:
                return self.reply(200, self.workspace.action(match[1], match[2], data))
            return self.reply(404, {"error": "Not found"})
        except (ValueError, KeyError, TypeError) as exc:
            # Input errors contain no provider responses or credentials.
            self.reply(400, {"error": str(exc) if isinstance(exc, ValueError) else "Invalid request"})
        except Exception:
            self.reply(500, {"error": "Could not save this action. Please try again."})

    def do_DELETE(self):
        if not self.authorized():
            return
        match = re.fullmatch(r"/api/runs/([a-f0-9]{32})", self.path)
        if not match:
            return self.reply(404, {"error": "Not found"})
        try:
            self.workspace.delete(match[1])
            return self.reply(204, b"", "text/plain")
        except ValueError as exc:
            return self.reply(404, {"error": str(exc)})


def main():
    parser = argparse.ArgumentParser(description="Run the local outreach workspace")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "comparison/artifacts/workspace")
    parser.add_argument("--public-origin", default=os.environ.get("PUBLIC_ORIGIN") or os.environ.get("RENDER_EXTERNAL_URL"),
                        help="Exact public HTTPS URL; requires DEMO_PASSWORD in the environment")
    args = parser.parse_args()
    load_project_env()
    if args.host not in ("localhost", "127.0.0.1", "::1") and not args.public_origin:
        parser.error("A non-loopback host requires --public-origin and DEMO_PASSWORD.")
    if args.public_origin:
        origin = urlparse(args.public_origin)
        if (origin.scheme != "https" or not origin.hostname or origin.username or origin.password
                or origin.path not in ("", "/") or origin.query or origin.fragment):
            parser.error("--public-origin must be an HTTPS origin, such as https://demo.example.com")
        Handler.public_origin = f"https://{origin.netloc}"
        Handler.demo_password = os.environ.get("DEMO_PASSWORD")
        if not Handler.demo_password or len(Handler.demo_password) < 16:
            parser.error("Set DEMO_PASSWORD to at least 16 characters before enabling a public demo.")
    Handler.workspace = Workspace(args.data_dir)
    try:
        server = WorkspaceServer((args.host, args.port), Handler)
    except OSError as exc:
        raise SystemExit(f"Cannot listen on port {args.port}. Stop the existing workspace or choose --port {args.port + 1}.") from exc
    print(f"Outreach workspace: http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
