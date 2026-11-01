"""Stage 14 tests: --e exact-phrase search and --es stemming search.

--e ("google bomb") returns only pages containing the words as one
consecutive, ordered phrase:
    "google bomb controversy"  -> matched
    "google bombing"           -> unmatched
    "bomb google"              -> unmatched
    "google controversial bomb" -> unmatched
No prefix expansion, no stemming, and no OR fallback.

--es Porter-stems each word, so inflected forms match: a search for
"google bomb" also finds "google bombing", and "cats" finds a page
with "cat" (which plain prefix matching cannot).
"""
import unittest

from helpers import IsolatedDbTestCase, main, run_cli


class PorterStemTests(unittest.TestCase):
    def test_inflected_forms_collapse(self):
        self.assertEqual(main._porter_stem("bombing"), "bomb")
        self.assertEqual(main._porter_stem("bomb"), "bomb")
        self.assertEqual(main._porter_stem("cats"), "cat")
        self.assertEqual(main._porter_stem("cat"), "cat")
        self.assertEqual(main._porter_stem("running"), "run")
        self.assertEqual(main._porter_stem("run"), "run")

    def test_short_words_unchanged(self):
        for word in ("a", "is", "be", "of", "to"):
            self.assertEqual(main._porter_stem(word), word)


class ExactPhraseSearchTests(IsolatedDbTestCase):
    def _seed(self):
        docs = {
            "http://x/p0": ("Exact", "google bomb"),
            "http://x/p1": ("Controversy", "google bomb controversy"),
            "http://x/p2": ("Bombing", "google bombing"),
            "http://x/p3": ("Reordered", "bomb google"),
            "http://x/p4": ("Interrupted", "google controversial bomb"),
            "http://x/p5": ("Uppercase", "Google Bomb history"),
            "http://x/p6": ("Unrelated", "a plain page here"),
        }
        for url, (title, body) in docs.items():
            self.insert_page(url, title, body=body)

    def test_exact_phrase_returns_only_consecutive_ordered_matches(self):
        self._seed()
        rows = main.run_search("google bomb", limit=20, exact=True)
        urls = {row[0] for row in rows}
        self.assertEqual(urls, {
            "http://x/p0",
            "http://x/p1",
            "http://x/p5",
        })

    def test_exact_phrase_is_case_insensitive(self):
        self._seed()
        rows = main.run_search("GOOGLE BOMB", limit=20, exact=True)
        urls = {row[0] for row in rows}
        self.assertIn("http://x/p5", urls)   # "Google Bomb"
        self.assertIn("http://x/p0", urls)

    def test_exact_phrase_no_or_fallback(self):
        self._seed()
        # "nonexistent zzz" does not appear as a phrase anywhere; even
        # though "zzz" alone would match p6 via OR, exact mode must
        # return nothing.
        self.insert_page("http://x/zzz", "Zzz", body="zzz is here")
        self.assertEqual(main.run_search(
            "nonexistent zzz", limit=20, exact=True), [])

    def test_exact_phrase_single_word(self):
        self._seed()
        rows = main.run_search("bomb", limit=20, exact=True)
        urls = {row[0] for row in rows}
        # The single token "bomb" matches any page containing it.
        self.assertIn("http://x/p3", urls)
        self.assertIn("http://x/p4", urls)
        self.assertNotIn("http://x/p6", urls)


class StemSearchTests(IsolatedDbTestCase):
    def test_stem_finds_inflected_forms(self):
        self.insert_page("http://x/c1", "Cat", body="a cat here")
        self.insert_page("http://x/c2", "Cats", body="many cats here")

        # Plain prefix search for "cats" only matches "cats", not "cat".
        plain = {row[0] for row in
                 main.run_search("cats", limit=20)}
        self.assertEqual(plain, {"http://x/c2"})

        # Stemming makes "cat" match both.
        stemmed = {row[0] for row in
                   main.run_search("cats", limit=20, stem=True)}
        self.assertEqual(stemmed, {"http://x/c1", "http://x/c2"})

    def test_user_example_google_bomb_matches_google_bombing(self):
        self.insert_page("http://x/a", "Bombing",
                         body="google bombing is a well-known tactic")
        self.insert_page("http://x/b", "Bomb",
                         body="the google bomb controversy was famous")
        self.insert_page("http://x/c", "Other",
                         body="programming in python is fun")

        stemmed = {row[0] for row in
                   main.run_search("google bomb", limit=20, stem=True)}
        self.assertEqual(stemmed, {"http://x/a", "http://x/b"})


class SearchCliTests(IsolatedDbTestCase):
    def test_exact_flag_prints_mode_and_runs(self):
        self.insert_page("http://x/p1", "Controversy",
                         body="google bomb controversy")
        code, out, _ = run_cli(["search", "google bomb", "--e"])
        self.assertIsNone(code)
        self.assertIn("Results for: google bomb (exact phrase)", out)
        self.assertIn("http://x/p1", out)

    def test_stem_flag_prints_mode_and_runs(self):
        self.insert_page("http://x/p2", "Bombing",
                         body="google bombing is a tactic")
        code, out, _ = run_cli(["search", "google bomb", "--es"])
        self.assertIsNone(code)
        self.assertIn("Results for: google bomb (stemmed)", out)
        self.assertIn("http://x/p2", out)

    def test_exact_and_stem_are_mutually_exclusive(self):
        self.insert_page("http://x/p1", "T", body="google bomb")
        code, _, err = run_cli(["search", "google bomb", "--e", "--es"])
        self.assertEqual(code, 2)
        self.assertIn("mutually exclusive", err)

    def test_flags_work_without_database(self):
        code, out, _ = run_cli(["search", "google bomb", "--e"])
        self.assertIsNone(code)
        self.assertIn("No database found", out)


if __name__ == "__main__":
    unittest.main()
