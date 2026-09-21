# InterviewPath demo on Render

Choose **Web Services → New Web Service**, connect the GitHub repository, and use:

| Setting | Value |
| --- | --- |
| Language | Python 3 |
| Branch | Your uploaded branch, usually `main` |
| Root directory | Leave blank |
| Build command | `pip install -r requirements.txt && pip install -e .` |
| Start command | `python -m provider_comparison.web --host 0.0.0.0 --data-dir /tmp/interviewpath-runs` |
| Instance type | Free |

Add these environment variables in Render before deploying:

- `PYTHON_VERSION`: `3.12.10`
- `FIRECRAWL_API_KEY`, `EXA_API_KEY`, `GEMINI_API_KEY`: your existing API keys.
- `DEMO_PASSWORD`: a random password of at least 16 characters. Share it with the evaluator alongside the link. The username is `demo`.

Enter secrets directly in Render's environment settings. Do not upload `.env` to
GitHub. The server uses Render's `PORT` and `RENDER_EXTERNAL_URL` automatically;
do not set `PUBLIC_ORIGIN` unless you are using a different public hostname.

After deployment, open the assigned HTTPS URL directly, sign in as `demo`, and
complete one prospect run: identify, confirm, research, draft, and review. Then
check regeneration with a source deselected and a public URL added. The service
only exposes application routes; repository files and API credentials are not
served. This is a shared evaluator workspace, not separate accounts for visitors.

If using **New → Blueprint** instead, `render.yaml` supplies the configuration and
generates `DEMO_PASSWORD`. Copy that value from Render's environment settings for
the evaluator. A manually created Web Service needs the settings above entered
explicitly.

## Free-tier behavior

Render's free service sleeps after 15 minutes without traffic and takes about a
minute to wake for the next visitor. Local run history is temporary and is lost
on sleep, restart, or redeploy. The stable URL remains usable, and a reviewer can
start a new run. API-provider usage is billed to your existing accounts.

For a two-day submission, send the evaluator the link, demo credentials, and a
short note that the first load may take about a minute. Do not rely on this tier
to preserve completed runs. See [Render's current free-tier limits](https://render.com/docs/free).

## Local or tunnel use

Local use remains `python -m provider_comparison.web` on port 8765.
For a tunnel, set `DEMO_PASSWORD` and pass `--public-origin https://your-exact-host`.
Use `--data-dir tmp/tunnel-demo` for a fresh workspace. Keep the server and tunnel
running; the allowed origin must match the tunnel URL exactly. No wildcard hosts
or arbitrary forwarded origins are accepted.
