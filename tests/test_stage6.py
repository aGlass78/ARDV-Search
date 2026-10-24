"""Stage 6 tests: the --h help flag.

help.txt was deleted; its content is now embedded in main.py as
HELP_TEXT and printed by `python main.py --h`:

- --h prints the guide (crawl section, search section, hardcoded
  values) and exits cleanly, without needing a subcommand.
- Invoking the program with no command at all still errors (exit 2),
  preserving the old required-subcommand behavior.
- --h wins over a subcommand when both are given (help is printed,
  nothing is dispatched).
"""
import unittest
from unittest import mock

from helpers import main, run_cli


class HelpFlagTests(unittest.TestCase):
    def test_h_prints_the_guide_and_exits_cleanly(self):
        code, out, _ = run_cli(["--h"])
        self.assertIsNone(code)
        # The guide's three parts, as updated by the project owner.
        self.assertIn("CRAWL Command", out)
        self.assertIn("SEARCH Command", out)
        self.assertIn("HARDCODED VALUES", out)

    def test_h_documents_itself(self):
        code, out, _ = run_cli(["--h"])
        self.assertIsNone(code)
        self.assertIn("python main.py --h", out)

    def test_h_wins_over_subcommands(self):
        with mock.patch.object(main, "crawl") as crawl, \
             mock.patch.object(main, "search") as search:
            code, out, _ = run_cli(["--h", "crawl", "http://x/"])
            self.assertIsNone(code)
            self.assertIn("CRAWL Command", out)
            crawl.assert_not_called()
            search.assert_not_called()

    def test_missing_command_still_errors(self):
        with mock.patch.object(main, "crawl") as crawl:
            code, out, err = run_cli([])
            self.assertEqual(code, 2)
            crawl.assert_not_called()
            self.assertIn("a command is required", err)


if __name__ == "__main__":
    unittest.main()
