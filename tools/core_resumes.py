#!/usr/bin/env python3
"""Client for core-resumes (https://core-resumes.hacolby.workers.dev), the career data store.

core-resumes already holds the candidate's job history: identity, career
stories, resume bullets, project artifacts with their public names and
"never say" codenames, career memories, tracked roles and companies. This
module reads it so the /setup, /apply and /interview workflows draft from that
record instead of from a re-typed profile.

Auth is `Authorization: Bearer <WORKER_API_KEY>`, resolved through
tools/colby_secrets.py (env var, then the `tokens` CLI).

Usage:
  python3 tools/core_resumes.py get <path>            # GET /api/<path>, print JSON
  python3 tools/core_resumes.py index [--out DIR]     # snapshot everything + career-digest.md
  python3 tools/core_resumes.py check <text-file>     # forbidden-term check (codenames)

`index` writes to documents/core-resumes/ by default. That folder is
gitignored: the origin of this repo is a public fork, and this is personal
data. Re-run it any time; every run replaces the snapshot.

No AI is called here. The digest is a deterministic rendering of the data.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from colby_secrets import require_secret  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BASE_URL = "https://core-resumes.hacolby.workers.dev"
DEFAULT_OUT = ROOT / "documents" / "core-resumes"
TIMEOUT = 60
USER_AGENT = "ai-job-search/core-resumes-client"

# name -> API path. Every one is a read. A failure on any of them fails the index:
# a snapshot with a silently missing section reads as complete when it is not.
SNAPSHOT = {
    "brief": "/api/career-context/brief",
    "bullets": "/api/bullets",
    "artifacts": "/api/career-context/artifacts",
    "glossary": "/api/career-context/glossary",
    "document_rules": "/api/career-context/document-rules",
    "level_expectations": "/api/career-context/level-expectations",
    "memory": "/api/memory?limit=500",
    "roles": "/api/roles",
    "companies": "/api/companies",
    "config": "/api/config",
}


class CoreResumesError(RuntimeError):
    pass


class CoreResumes:
    def __init__(self, base_url: str = BASE_URL, api_key: str | None = None):
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key

    def _key(self) -> str:
        if self._api_key is None:
            self._api_key = require_secret("WORKER_API_KEY")
        return self._api_key

    def request(self, method: str, path: str, body=None):
        if not path.startswith("/"):
            path = "/" + path
        if not path.startswith("/api/") and path != "/openapi.json":
            path = "/api" + path
        data = json.dumps(body).encode() if body is not None else None
        req = Request(self.base_url + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._key()}")
        req.add_header("Accept", "application/json")
        # Cloudflare answers the default Python-urllib agent with error 1010.
        req.add_header("User-Agent", USER_AGENT)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
        except HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            raise CoreResumesError(f"{method} {path} -> HTTP {exc.code}: {detail}") from None
        except URLError as exc:
            raise CoreResumesError(f"{method} {path} -> {exc.reason}") from None
        try:
            return json.loads(raw)
        except ValueError:
            raise CoreResumesError(f"{method} {path} -> response is not JSON") from None

    def get(self, path: str):
        return self.request("GET", path)

    # Named reads the workflows use.
    def brief(self) -> dict:
        return self.get("/api/career-context/brief")["brief"]

    def check_text(self, text: str) -> dict:
        """Ask core-resumes whether text contains a forbidden term (an internal codename)."""
        return self.request("POST", "/api/career-context/check", {"text": text})


def snapshot(client: CoreResumes) -> dict:
    return {name: client.get(path) for name, path in SNAPSHOT.items()}


# ---------------------------------------------------------------- digest


def _items(value, *keys):
    """Return the list inside a response that may be a bare list or {key: [...]}."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in keys:
            if isinstance(value.get(key), list):
                return value[key]
    return []


def _line(text) -> str:
    return " ".join(str(text).split())


def render_digest(data: dict, fetched_at: str) -> str:
    brief = data["brief"].get("brief", data["brief"])
    ident = brief.get("identity", {})
    out = [
        "# Career digest (from core-resumes)",
        "",
        f"Snapshot taken {fetched_at} by `python3 tools/core_resumes.py index`.",
        "Source of truth is core-resumes; re-run the index to refresh. Do not edit by hand.",
        "",
        "## Identity",
        "",
    ]
    for key in ("fullName", "location", "email", "phone", "linkedin"):
        if ident.get(key):
            out.append(f"- **{key}:** {ident[key]}")
    out.append("")

    stories = ident.get("careerStories")
    if stories:
        out += ["## Career stories", "", stories.strip(), ""]

    context = brief.get("context") or {}
    if context.get("forbidden"):
        out += ["## Forbidden terms (never print)", "",
                ", ".join(str(t.get("term", t)) if isinstance(t, dict) else str(t)
                          for t in context["forbidden"]), ""]

    for key, value in brief.items():
        # context repeats the artifacts and glossary rendered below.
        if key in ("identity", "context") or not value:
            continue
        out += [f"## Brief: {key}", ""]
        if isinstance(value, str):
            out += [value.strip(), ""]
        else:
            out += ["```json", json.dumps(value, indent=1, ensure_ascii=False)[:20000], "```", ""]

    artifacts = _items(data["artifacts"], "artifacts")
    if artifacts:
        out += ["## Projects and artifacts", "",
                "Use `publicName` in documents. Never write a term from `neverSay`.", ""]
        for art in artifacts:
            if art.get("isActive") is False:
                continue
            out.append(f"### {art.get('publicName') or art.get('internalName')}")
            if art.get("neverSay"):
                out.append(f"- **Never say:** {', '.join(art['neverSay'])}")
            for key in ("whatItWas", "whyItMattered"):
                if art.get(key):
                    out.append(f"- **{key}:** {_line(art[key])}")
            for br in art.get("branches") or []:
                if br.get("isActive") is False:
                    continue
                metrics = f" ({_line(br['keyMetrics'])})" if br.get("keyMetrics") else ""
                out.append(f"- *{br.get('angle')}:* {_line(br.get('narrative', ''))}{metrics}")
            out.append("")

    bullets = [b for b in _items(data["bullets"], "active", "bullets") if b.get("isActive", True)]
    if bullets:
        out += ["## Resume bullets (active)", ""]
        by_cat: dict[str, list] = {}
        for b in bullets:
            by_cat.setdefault(b.get("category") or "Uncategorized", []).append(b)
        for cat in sorted(by_cat):
            out += [f"### {cat}", ""]
            for b in by_cat[cat]:
                metric = f" [{b['impactMetric']}]" if b.get("impactMetric") else ""
                tags = f" _tags: {b['tags']}_" if b.get("tags") else ""
                out.append(f"- (#{b.get('id')}) {_line(b.get('content', ''))}{metric}{tags}")
            out.append("")

    memories = _items(data["memory"], "memories", "items", "results")
    facts = [m for m in memories if m.get("category") in ("career_fact", "general")]
    if facts:
        out += ["## Career memories (facts)", ""]
        for m in facts:
            out += [f"### {_line(m.get('query') or '')}", "", (m.get("answer") or "").strip(), ""]
        out.append("")

    glossary = _items(data["glossary"], "terms", "glossary")
    if glossary:
        out += ["## Glossary", ""]
        for g in glossary:
            if g.get("isActive") is False:
                continue
            swap = ""
            if g.get("neverRender"):
                swap = f" **Never render; write:** {g.get('suggestedReplacement') or '(omit)'}"
            out.append(f"- **{g.get('term')}:** {_line(g.get('plainEnglish') or '')}{swap}")
        out.append("")

    rules = _items(data["document_rules"], "rules", "documentRules")
    if rules:
        out += ["## Standing document rules", ""]
        for r in rules:
            if r.get("isActive") is False:
                continue
            out.append(f"- {_line(r.get('rule') or r.get('content') or r.get('text') or json.dumps(r))}")
        out.append("")

    roles = _items(data["roles"], "roles")
    if roles:
        out += ["## Roles already tracked in core-resumes", "",
                "Check this list before starting /apply: the role may already be in progress.", ""]
        for r in roles:
            pay = ""
            if r.get("salaryMin") or r.get("salaryMax"):
                lo, hi = r.get("salaryMin"), r.get("salaryMax")
                pay = f" ({lo or 0:,}-{hi or 0:,} {r.get('salaryCurrency') or ''})"
            out.append(f"- {r.get('companyName')} - {r.get('jobTitle')} - status `{r.get('status')}`{pay}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def write_index(data: dict, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    written = []
    for name, value in data.items():
        path = out_dir / f"{name}.json"
        path.write_text(json.dumps(value, indent=1, ensure_ascii=False), encoding="utf-8")
        written.append(path)
    digest = out_dir / "career-digest.md"
    digest.write_text(render_digest(data, fetched_at), encoding="utf-8")
    written.append(digest)
    (out_dir / "INDEX.json").write_text(json.dumps({
        "source": BASE_URL, "fetched_at": fetched_at,
        "files": {p.name: p.stat().st_size for p in written},
    }, indent=1), encoding="utf-8")
    return written


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("get")
    g.add_argument("path")
    i = sub.add_parser("index")
    i.add_argument("--out", type=Path, default=DEFAULT_OUT)
    c = sub.add_parser("check")
    c.add_argument("file", type=Path)
    args = ap.parse_args(argv)

    client = CoreResumes()
    try:
        if args.cmd == "get":
            print(json.dumps(client.get(args.path), indent=1, ensure_ascii=False))
        elif args.cmd == "check":
            print(json.dumps(client.check_text(args.file.read_text(encoding="utf-8")), indent=1))
        else:
            data = snapshot(client)
            for path in write_index(data, args.out):
                print(f"wrote {path} ({path.stat().st_size:,} bytes)")
    except CoreResumesError as exc:
        print(f"core-resumes: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
