"""Stage 9 tests: contact email from .env, with graceful fallbacks.

The User-Agent is assembled from a base string plus an optional
contact address read from .env (next to main.py):

- .env missing, unreadable, key absent, or value empty -> the agent
  is "ModelSearchEngine (educational crawler)" with no contact section
- CONTACT_EMAIL set in .env -> "; contact: <address>" is appended

Nothing about the address is hardcoded in the program.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import ServerTestCase, main, page

BASE_AGENT = "ModelSearchEngine (educational crawler)"


class LoadEnvValueTests(unittest.TestCase):
    def _write_env(self, content):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = Path(tmp.name) / ".env"
        env.write_text(content, encoding="utf-8")
        return env

    def test_reads_simple_value(self):
        env = self._write_env("CONTACT_EMAIL=you@example.com\n")
        self.assertEqual(
            main.load_env_value("CONTACT_EMAIL", env), "you@example.com")

    def test_missing_file_returns_none(self):
        self.assertIsNone(
            main.load_env_value("CONTACT_EMAIL", Path("does-not-exist.env")))

    def test_missing_key_returns_none(self):
        env = self._write_env("OTHER_KEY=x\n# CONTACT_EMAIL commented\n")
        self.assertIsNone(main.load_env_value("CONTACT_EMAIL", env))

    def test_empty_value_returns_none(self):
        env = self._write_env("CONTACT_EMAIL=\n")
        self.assertIsNone(main.load_env_value("CONTACT_EMAIL", env))
        env = self._write_env("CONTACT_EMAIL=   \n")
        self.assertIsNone(main.load_env_value("CONTACT_EMAIL", env))

    def test_quotes_and_spaces_stripped(self):
        env = self._write_env('CONTACT_EMAIL = "you@example.com"\n')
        self.assertEqual(
            main.load_env_value("CONTACT_EMAIL", env), "you@example.com")

    def test_last_definition_wins(self):
        env = self._write_env(
            "CONTACT_EMAIL=first@example.com\nCONTACT_EMAIL=last@example.com\n")
        self.assertEqual(
            main.load_env_value("CONTACT_EMAIL", env), "last@example.com")

    def test_comments_blanks_and_noise_ignored(self):
        env = self._write_env(
            "# a comment\n\nnot an assignment\nCONTACT_EMAIL=you@example.com\n")
        self.assertEqual(
            main.load_env_value("CONTACT_EMAIL", env), "you@example.com")


class BuildUserAgentTests(unittest.TestCase):
    def test_with_email(self):
        self.assertEqual(
            main.build_user_agent("you@example.com"),
            f"{BASE_AGENT[:-1]}; contact: you@example.com)")

    def test_without_email(self):
        self.assertEqual(main.build_user_agent(None), BASE_AGENT)
        self.assertEqual(main.build_user_agent(""), BASE_AGENT)
        self.assertEqual(main.build_user_agent("   "), BASE_AGENT)


class UserAgentFallbackTests(ServerTestCase):
    def test_crawl_without_contact_uses_the_fallback_agent(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/seed", page("Seed", "seed body"))

        # Simulate the no-.env / empty-value environment.
        with mock.patch.object(main, "USER_AGENT", main.build_user_agent(None)):
            self.crawl(s.url("/seed"), max_pages=5, delay=0.02)

        self.assertGreater(len(s.user_agents), 0)
        for agent in s.user_agents:
            self.assertEqual(agent, BASE_AGENT)
            self.assertNotIn("contact:", agent)

    def test_crawl_with_contact_uses_the_full_agent(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/seed", page("Seed", "seed body"))

        full = main.build_user_agent("you@example.com")
        with mock.patch.object(main, "USER_AGENT", full):
            self.crawl(s.url("/seed"), max_pages=5, delay=0.02)

        self.assertGreater(len(s.user_agents), 0)
        for agent in s.user_agents:
            self.assertEqual(agent, full)

    def test_local_env_file_drives_the_default_agent(self):
        # The repo checkout carries a .env; whatever it says, the
        # module-level agent must match building it from that value.
        self.assertEqual(
            main.USER_AGENT,
            main.build_user_agent(
                main.load_env_value("CONTACT_EMAIL", main.ENV_PATH)))


if __name__ == "__main__":
    unittest.main()
