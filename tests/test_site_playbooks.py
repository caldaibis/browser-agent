from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src import site_playbooks


class _FakeAgentResult(SimpleNamespace):
    pass


def _result(outcome="incomplete", transcript_path="", resolved_url=""):
    return _FakeAgentResult(outcome=outcome, transcript_path=transcript_path,
                            resolved_url=resolved_url)


class TestDomainFor(unittest.TestCase):
    def test_strips_www_and_lowercases(self):
        self.assertEqual(site_playbooks.domain_for("https://WWW.ReboGroep.nl/x?y=1"),
                         "rebogroep.nl")

    def test_unparseable_is_empty(self):
        self.assertEqual(site_playbooks.domain_for(""), "")
        self.assertEqual(site_playbooks.domain_for("not a url"), "")


class TestLoad(unittest.TestCase):
    def test_roundtrip_and_missing(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(site_playbooks, "PLAYBOOK_DIR", Path(td)):
                self.assertIsNone(site_playbooks.load("rebogroep.nl"))
                (Path(td) / "rebogroep.nl.md").write_text(
                    "- the real apply button is 'Bezichtiging aanvragen'\n",
                    encoding="utf-8")
                text = site_playbooks.load("rebogroep.nl")
                self.assertIn("Bezichtiging aanvragen", text)
                domain, loaded = site_playbooks.load_for_url(
                    "https://www.rebogroep.nl/nl/aanbod/x")
                self.assertEqual(domain, "rebogroep.nl")
                self.assertIn("Bezichtiging aanvragen", loaded)


class TestUpdateAfterRun(unittest.TestCase):
    def test_yielded_and_missing_transcript_are_noops(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(site_playbooks, "PLAYBOOK_DIR", Path(td)):
                site_playbooks.update_after_run(
                    {"source_url": "https://x.nl/a"}, _result(outcome="yielded"))
                site_playbooks.update_after_run(
                    {"source_url": "https://x.nl/a"},
                    _result(transcript_path=str(Path(td) / "missing.log")))
                self.assertEqual(list(Path(td).glob("*.md")), [])

    def test_update_never_raises(self):
        """A playbook is a bonus: even a crashing LLM client must not turn a
        finished apply into an error."""
        with tempfile.TemporaryDirectory() as td:
            transcript = Path(td) / "run.log"
            transcript.write_text("turn 1 ...", encoding="utf-8")
            with patch.object(site_playbooks, "PLAYBOOK_DIR", Path(td)), \
                 patch.dict(os.environ, {"DEEPSEEK_API_KEY": "k"}), \
                 patch("openai.OpenAI", side_effect=RuntimeError("no network")):
                site_playbooks.update_after_run(
                    {"source_url": "https://x.nl/a"},
                    _result(transcript_path=str(transcript)))  # must not raise

    def test_writes_playbooks_for_source_and_resolved_domains(self):
        with tempfile.TemporaryDirectory() as td:
            transcript = Path(td) / "run.log"
            transcript.write_text("turn 1: clicked 'Bezichtiging aanvragen'",
                                  encoding="utf-8")

            class _FakeClient:
                def __init__(self, **kw):
                    self.chat = SimpleNamespace(completions=self)

                def create(self, **kw):
                    msg = SimpleNamespace(content="- durable site lesson")
                    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

            with patch.object(site_playbooks, "PLAYBOOK_DIR", Path(td)), \
                 patch.dict(os.environ, {"DEEPSEEK_API_KEY": "k"}), \
                 patch("openai.OpenAI", _FakeClient):
                site_playbooks.update_after_run(
                    {"source_url": "https://www.huurwoningen.nl/huren/x"},
                    _result(outcome="submitted",
                            transcript_path=str(transcript),
                            resolved_url="https://www.rebogroep.nl/nl/aanbod/x"))
            for domain in ("huurwoningen.nl", "rebogroep.nl"):
                text = (Path(td) / f"{domain}.md").read_text(encoding="utf-8")
                self.assertIn("durable site lesson", text)


class TestPromptInjection(unittest.TestCase):
    def test_build_prompt_includes_playbook_when_present(self):
        from src.apply import build_prompt
        listing = {"source_url": "https://www.example.test/x",
                   "address": "Teststraat 1", "price": "EUR 1500",
                   "source_name": "Kamernet"}
        with tempfile.TemporaryDirectory() as td:
            with patch.object(site_playbooks, "PLAYBOOK_DIR", Path(td)):
                self.assertNotIn("SITE PLAYBOOK", build_prompt(listing))
                (Path(td) / "example.test.md").write_text(
                    "- login is Google SSO only\n", encoding="utf-8")
                prompt = build_prompt(listing)
                self.assertIn("SITE PLAYBOOK for example.test", prompt)
                self.assertIn("login is Google SSO only", prompt)


if __name__ == "__main__":
    unittest.main()
