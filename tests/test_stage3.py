"""Stage 3 tests: search improvements.

1. The arbitrary 50-result cap is gone; any positive limit works.
2. FTS5 + BM25 stay the ranking engine.
3. Matching is predictable: every word must match (AND) with prefix
   expansion, falling back to OR when AND finds nothing. Malformed
   user input can never crash or inject FTS5 syntax.
4. Searches return more than 10 results when enough documents match.
"""
import contextlib
import io
import unittest
from unittest import mock

from helpers import IsolatedDbTestCase, main, run_cli


class BuildMatchTests(unittest.TestCase):
    def test_and_terms(self):
        self.assertEqual(main.build_match(["cat", "dog"]),
                         '"cat"* AND "dog"*')

    def test_or_terms(self):
        self.assertEqual(main.build_match(["cat", "dog"], "OR"),
                         '"cat"* OR "dog"*')

    def test_single_term(self):
        self.assertEqual(main.build_match(["cat"]), '"cat"*')

    def test_empty_words(self):
        self.assertEqual(main.build_match([]), "")


class ExtractWordsTests(unittest.TestCase):
    def test_punctuation_removed(self):
        self.assertEqual(main.extract_words("the cat's toys!"),
                         ["the", "cat", "s", "toys"])

    def test_only_punctuation_yields_nothing(self):
        self.assertEqual(main.extract_words("*** -"), [])

    def test_fts_operators_neutralized(self):
        # Quotes, colons and parens vanish: no FTS5 syntax survives.
        self.assertEqual(main.extract_words('NEAR( a "b" ) -c AND'),
                         ["NEAR", "a", "b", "c", "AND"])

    def test_unicode_preserved(self):
        self.assertEqual(main.extract_words("café zürich"),
                         ["café", "zürich"])


class RunSearchTests(IsolatedDbTestCase):
    def setUp(self):
        super().setUp()
        self.insert_page("http://a/cat", title="A", body="the cat sat on the mat")
        self.insert_page("http://b/dog", title="B", body="the dog ran far")
        self.insert_page("http://c/both", title="C", body="the cat and the dog ran")

    def urls(self, query, limit=10):
        return sorted(r[0] for r in main.run_search(query, limit=limit))

    def test_and_requires_all_words(self):
        # Old behavior (OR) returned all three pages here.
        self.assertEqual(self.urls("cat dog"), ["http://c/both"])

    def test_prefix_matching_finds_longer_word_forms(self):
        # Searching "cat" must also find a page that only says "cats".
        self.insert_page("http://d/cats", title="D", body="many cats here")
        self.assertIn("http://d/cats", self.urls("cat"))

    def test_or_fallback_when_no_page_has_all_words(self):
        self.assertEqual(self.urls("dog zebra"),
                         ["http://b/dog", "http://c/both"])

    def test_no_results(self):
        self.assertEqual(main.run_search("zebra", limit=10), [])

    def test_empty_or_punctuation_query_is_safe(self):
        self.assertEqual(main.run_search("", limit=10), [])
        self.assertEqual(main.run_search("***", limit=10), [])

    def test_malformed_fts_input_never_crashes(self):
        for bad in ('"', "NEAR(", "col:match", "-not", "AND", "*"):
            self.assertIsInstance(main.run_search(bad, limit=10), list)

    def test_fts_keyword_word_searches_as_literal(self):
        # "AND" is an FTS5 keyword, but also a plain English word:
        # it must search like any other term, not error out.
        self.assertEqual(self.urls("AND"), ["http://c/both"])

    def test_limit_below_one_returns_nothing(self):
        self.assertEqual(main.run_search("cat", limit=0), [])
        self.assertEqual(main.run_search("cat", limit=-5), [])

    def test_title_matches_rank_first(self):
        self.insert_page("http://e/bodyhit", title="E",
                         body="zebra mentioned once here")
        self.insert_page("http://f/titlehit", title="zebra central",
                         body="nothing relevant in this body")
        rows = main.run_search("zebra", limit=10)
        self.assertEqual(rows[0][0], "http://f/titlehit")

    def test_returns_more_than_ten_results(self):
        for i in range(15):
            self.insert_page(f"http://x/{i}", title=f"Doc {i}",
                             body=f"commonword page number {i}")
        rows = main.run_search("commonword", limit=15)
        self.assertEqual(len(rows), 15)

    def test_fifty_result_cap_removed(self):
        for i in range(60):
            self.insert_page(f"http://y/{i}", title=f"Doc {i}",
                             body=f"commonword page number {i}")
        rows = main.run_search("commonword", limit=60)
        self.assertEqual(len(rows), 60)


class SearchOutputTests(IsolatedDbTestCase):
    def test_search_prints_results(self):
        self.insert_page("http://a/cat", title="Cat page", body="cat content")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main.search("cat", limit=5)
        text = out.getvalue()
        self.assertIn("Cat page", text)
        self.assertIn("http://a/cat", text)

    def test_no_results_message(self):
        self.insert_page("http://a/cat", title="Cat page", body="cat content")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main.search("zebra", limit=5)
        self.assertIn("No results found.", out.getvalue())

    def test_empty_query_keeps_old_message(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main.search("!!!", limit=5)
        self.assertIn("Enter a search term.", out.getvalue())


class LimitValidationTests(unittest.TestCase):
    def test_zero_limit_rejected(self):
        with mock.patch.object(main, "search") as search:
            code, _, _ = run_cli(["search", "word", "--limit", "0"])
            self.assertEqual(code, 2)
            search.assert_not_called()

    def test_negative_limit_rejected(self):
        with mock.patch.object(main, "search") as search:
            code, _, _ = run_cli(["search", "word", "--limit", "-3"])
            self.assertEqual(code, 2)
            search.assert_not_called()

    def test_large_limit_accepted_and_passed_through(self):
        with mock.patch.object(main, "search") as search:
            code, _, _ = run_cli(["search", "word", "--limit", "75"])
            self.assertIsNone(code)
            search.assert_called_once_with("word", 75)


if __name__ == "__main__":
    unittest.main()
