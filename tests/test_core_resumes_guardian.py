"""Offline tests for tools/core_resumes.py and tools/guardian.py.

No network: the HTTP layer is replaced, matching the repo's CI policy of
making no live requests. The cases pin the failures that would be invisible:
a snapshot that silently drops a section, codenames lost from the digest,
personal data written somewhere git tracks, and an empty AI answer that exits 0.
"""

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import core_resumes  # noqa: E402
import guardian  # noqa: E402

DATA = {
    "brief": {"brief": {
        "identity": {"fullName": "Ada Example", "email": "ada@example.com", "careerStories": "STORY #1"},
        "positions": [{"employer": "Acme", "title": "Lead PM"}],
        "context": {"forbidden": [{"term": "Project Falcon"}, "DeskSpace"], "artifacts": ["dup"]},
    }},
    "bullets": {"active": [
        {"id": 1, "content": "Shipped the intake portal.", "category": "Technical", "isActive": True},
        {"id": 2, "content": "Retired bullet.", "category": "Technical", "isActive": False},
    ]},
    "artifacts": {"artifacts": [{"publicName": "matter platform", "internalName": "DeskSpace",
                                 "neverSay": ["DeskSpace"], "branches": []}]},
    "glossary": {"terms": [{"term": "20% project", "plainEnglish": "side time",
                            "neverRender": True, "suggestedReplacement": "concurrent assignment"}]},
    "document_rules": {"rules": [{"rule": "Name the spine."}]},
    "level_expectations": [],
    "memory": {"items": [{"category": "career_fact", "query": "Key skills?", "answer": "SQL and Python."}]},
    "roles": [{"companyName": "Beta", "jobTitle": "TPM", "status": "preparing",
               "salaryMin": 150000, "salaryMax": 200000, "salaryCurrency": "USD"}],
    "companies": [],
    "config": [],
}


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.md = core_resumes.render_digest(DATA, "2026-10-19T00:00:00Z")

    def test_codenames_are_listed_as_forbidden(self):
        self.assertIn("## Forbidden terms (never print)", self.md)
        self.assertIn("Project Falcon, DeskSpace", self.md)
        self.assertIn("**Never say:** DeskSpace", self.md)

    def test_glossary_swap_is_rendered(self):
        self.assertIn("**Never render; write:** concurrent assignment", self.md)

    def test_inactive_bullets_are_left_out(self):
        self.assertIn("Shipped the intake portal.", self.md)
        self.assertNotIn("Retired bullet.", self.md)

    def test_memory_answers_and_tracked_roles_are_rendered(self):
        self.assertIn("SQL and Python.", self.md)
        self.assertIn("Beta - TPM - status `preparing` (150,000-200,000 USD)", self.md)

    def test_context_section_is_not_dumped_twice(self):
        self.assertNotIn("## Brief: context", self.md)


class SnapshotTests(unittest.TestCase):
    def test_one_failing_endpoint_fails_the_whole_index(self):
        client = core_resumes.CoreResumes(api_key="k")

        def get(path):
            if "glossary" in path:
                raise core_resumes.CoreResumesError("GET glossary -> HTTP 500")
            return {}
        with mock.patch.object(client, "get", side_effect=get):
            with self.assertRaises(core_resumes.CoreResumesError):
                core_resumes.snapshot(client)

    def test_index_writes_digest_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = core_resumes.write_index(DATA, Path(tmp))
            names = {p.name for p in written}
            self.assertIn("career-digest.md", names)
            manifest = json.loads((Path(tmp) / "INDEX.json").read_text())
            self.assertEqual(manifest["source"], core_resumes.BASE_URL)

    def test_snapshot_folder_and_local_profile_are_gitignored(self):
        for path in ("documents/core-resumes/brief.json", "documents/core-resumes/career-digest.md",
                     "CLAUDE.local.md"):
            res = subprocess.run(["git", "check-ignore", "-q", path], cwd=REPO_ROOT)
            self.assertEqual(res.returncode, 0, f"{path} is not gitignored")


class GuardianTests(unittest.TestCase):
    def test_extract_text_handles_each_provider_shape(self):
        self.assertEqual(guardian.extract_text({"choices": [{"message": {"content": "a"}}]}), "a")
        self.assertEqual(guardian.extract_text({"content": [{"type": "text", "text": "b"}]}), "b")
        self.assertEqual(guardian.extract_text(
            {"candidates": [{"content": {"parts": [{"text": "c"}]}}]}), "c")

    def run_main(self, response=None, error=None):
        def post(path, token, body):
            if error:
                raise error
            self.sent = body
            return response
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(guardian, "_post", side_effect=post), \
                mock.patch.object(guardian, "require_secret", return_value="t"), \
                redirect_stdout(out), redirect_stderr(err):
            code = guardian.main(["chat", "hello", "--use-case", "rag"])
        return code, out.getvalue(), err.getvalue()

    def test_routes_with_project_and_hints(self):
        code, out, err = self.run_main({"status": 200, "provider": "p", "model": "m", "cost_usd": 0,
                                        "body": {"choices": [{"message": {"content": "hi"}}]}})
        self.assertEqual((code, out.strip()), (0, "hi"))
        self.assertEqual(self.sent["project"], "ai-job-search")
        self.assertEqual(self.sent["use_case"], "rag")
        self.assertIn("guardian: p/m", err)

    def test_empty_answer_is_an_error_not_a_blank_success(self):
        code, out, err = self.run_main({"status": 200, "body": {"choices": [{"message": {"content": ""}}]}})
        self.assertEqual(code, 2)
        self.assertEqual(out, "")

    def test_breaker_refusal_exits_3(self):
        code, _, err = self.run_main(error=guardian.GuardianError("HTTP 429: breaker", refused=True))
        self.assertEqual(code, 3)
        self.assertIn("429", err)


if __name__ == "__main__":
    unittest.main()
