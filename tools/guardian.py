#!/usr/bin/env python3
"""Run low-judgment AI work through core-guardian instead of spending Claude tokens.

core-guardian (https://core-guardian.hacolby.workers.dev) is the AI router for
every project on this account: it picks the cheapest capable model (often a
free tier), meters the spend, attributes it to a project and enforces the
budget circuit breakers. A direct provider call bypasses all of that, so this
module is the only door this repo uses for AI work outside Claude Code.

Use it for bulk, mechanical text work: summarize a posting, extract keywords,
classify a batch of scraped jobs, normalize a title. Keep judgment work (fit
evaluation, CV and cover letter drafting, interview prep) in Claude.

Auth: POST /api/ai-router/run takes `Bearer <CLOUDFLARE_AI_GATEWAY_TOKEN>`
(the inference door; WORKER_API_KEY is refused there). Resolved through
tools/colby_secrets.py.

Usage:
  python3 tools/guardian.py chat "Summarize this in 3 bullets: ..."
  python3 tools/guardian.py chat --file posting.txt --system "Extract the required skills as a JSON list."
  python3 tools/guardian.py chat --use-case rag --complexity medium --file long.txt "Question?"

Prints the model's text on stdout and one metering line on stderr:
  guardian: ollama/kimi-k2.6 in=13 out=43 cost=$0.0000 request=<uuid>
--json prints the full router response instead.

Exit codes: 0 answered; 2 usage, auth or transport error; 3 the router
refused (budget breaker, kill switch, or no model inside the budget).
"""

import argparse
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from colby_secrets import require_secret  # noqa: E402

BASE_URL = "https://core-guardian.hacolby.workers.dev"
PROJECT = "ai-job-search"
REPO = "jmbish04/ai-job-search"
USER_AGENT = "ai-job-search/guardian-client"
TIMEOUT = 180
USE_CASES = ("general", "chat", "rag", "coding", "web_search", "deep_research", "agentic")
LEVELS = ("low", "medium", "high")
# A posting or a scraped batch is the input here; anything bigger is a sign the
# caller meant to send a file listing, not its content.
MAX_INPUT_CHARS = 200_000


class GuardianError(RuntimeError):
    def __init__(self, message: str, refused: bool = False):
        super().__init__(message)
        self.refused = refused


def _post(path: str, token: str, body: dict) -> dict:
    req = Request(BASE_URL + path, data=json.dumps(body).encode(), method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    # Cloudflare answers the default Python-urllib agent with error 1010.
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read())
    except HTTPError as exc:
        detail = exc.read()[:500].decode("utf-8", "replace")
        # 422 no_model_in_budget, 429 breaker / kill switch: the router said no.
        raise GuardianError(f"HTTP {exc.code}: {detail}", refused=exc.code in (422, 429)) from None
    except URLError as exc:
        raise GuardianError(f"{exc.reason}") from None
    except ValueError:
        raise GuardianError("response is not JSON") from None


def extract_text(body) -> str:
    """Pull the answer text out of whichever provider shape the router returned."""
    if isinstance(body, str):
        return body
    if not isinstance(body, dict):
        return ""
    choices = body.get("choices")
    if choices:  # OpenAI-compatible
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            return "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return content or choices[0].get("text") or ""
    if isinstance(body.get("content"), list):  # Anthropic messages
        return "".join(p.get("text", "") for p in body["content"] if p.get("type") == "text")
    candidates = body.get("candidates")
    if candidates:  # Gemini
        parts = (candidates[0].get("content") or {}).get("parts") or []
        return "".join(p.get("text", "") for p in parts)
    for key in ("response", "output_text", "text", "result"):
        if isinstance(body.get(key), str):
            return body[key]
    return ""


def chat(prompt: str, *, system: str | None = None, use_case: str = "general",
         complexity: str = "low", importance: str = "low", free_only: bool = False,
         task: str | None = None) -> dict:
    """Route one completion through core-guardian. Returns the router response plus `text`."""
    if len(prompt) > MAX_INPUT_CHARS:
        raise GuardianError(f"input is {len(prompt):,} chars; the cap is {MAX_INPUT_CHARS:,}")
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body = {
        "project": PROJECT, "repo": REPO,
        "importance": importance, "use_case": use_case, "complexity": complexity,
        "task": task or f"ai-job-search:{use_case}",
        "input": {"messages": messages},
    }
    if free_only:
        body["free_only"] = True
    result = _post("/api/ai-router/run", require_secret("CLOUDFLARE_AI_GATEWAY_TOKEN"), body)
    if result.get("status") not in (200, None):
        raise GuardianError(f"router status {result.get('status')}: {json.dumps(result)[:500]}",
                            refused=result.get("status") in (422, 429))
    result["text"] = extract_text(result.get("body")).strip()
    if not result["text"]:
        # An empty answer that exits 0 reads as "the model had nothing to say".
        raise GuardianError(f"no text in the router response from {result.get('provider')}/{result.get('model')}")
    return result


def meter_line(result: dict) -> str:
    cost = result.get("cost_usd") or 0
    return (f"guardian: {result.get('provider')}/{result.get('model')} "
            f"in={result.get('tokens_in') or 0:,} out={result.get('tokens_out') or 0:,} "
            f"cost=${cost:.4f} request={result.get('request_uuid')}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("chat", help="one routed completion")
    c.add_argument("prompt", nargs="?", default="")
    c.add_argument("--file", type=Path, help="append this file's text to the prompt")
    c.add_argument("--system")
    c.add_argument("--use-case", choices=USE_CASES, default="general")
    c.add_argument("--complexity", choices=LEVELS, default="low")
    c.add_argument("--importance", choices=LEVELS, default="low")
    c.add_argument("--free-only", action="store_true", help="refuse anything that costs money")
    c.add_argument("--json", action="store_true", help="print the full router response")
    args = ap.parse_args(argv)

    prompt = args.prompt
    if args.file:
        text = args.file.read_text(encoding="utf-8", errors="replace")
        # The file is untrusted input (a posting, a scrape): fence it as data.
        prompt = f"{prompt}\n\n<document>\n{text}\n</document>" if prompt else text
    if not prompt.strip():
        ap.error("give a prompt, --file, or both")
    try:
        result = chat(prompt, system=args.system, use_case=args.use_case,
                      complexity=args.complexity, importance=args.importance,
                      free_only=args.free_only)
    except GuardianError as exc:
        print(f"guardian: {exc}", file=sys.stderr)
        return 3 if exc.refused else 2
    print(json.dumps(result, indent=1, ensure_ascii=False) if args.json else result["text"])
    print(meter_line(result), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
