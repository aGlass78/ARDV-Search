"""Stage 15: sitemap discovery (robots declaration + common places)."""
import unittest
from unittest import mock

import main
from helpers import ServerTestCase, page, run_cli

SM_NS = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'


def urlset_xml(*locs, ns=False):
    tag = f"<urlset {SM_NS}>" if ns else "<urlset>"
    inner = "".join(f"<url><loc>{x}</loc></url>" for x in locs)
    return f'<?xml version="1.0" encoding="UTF-8"?>{tag}{inner}</urlset>'


def sitemapindex_xml(*children):
    inner = "".join(
        f"<sitemap><loc>{x}</loc></sitemap>" for x in children)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f"<sitemapindex>{inner}</sitemapindex>")


class ParseSitemapXmlTests(unittest.TestCase):
    def test_rejects_garbage_and_empty(self):
        self.assertIsNone(main.parse_sitemap_xml(b"not xml at all"))
        self.assertIsNone(main.parse_sitemap_xml(b""))
        self.assertIsNone(main.parse_sitemap_xml(b"<html><body/></html>"))

    def test_urlset_kind_and_locs(self):
        data = urlset_xml("http://a/x", "http://a/y").encode()
        kind, locs = main.parse_sitemap_xml(data)
        self.assertEqual(kind, "urlset")
        self.assertEqual(locs, ["http://a/x", "http://a/y"])

    def test_sitemapindex_kind(self):
        data = sitemapindex_xml("http://a/1.xml").encode()
        kind, locs = main.parse_sitemap_xml(data)
        self.assertEqual(kind, "sitemapindex")
        self.assertEqual(locs, ["http://a/1.xml"])

    def test_namespaced_documents_parse(self):
        data = urlset_xml("http://a/x", ns=True).encode()
        kind, locs = main.parse_sitemap_xml(data)
        self.assertEqual(kind, "urlset")
        self.assertEqual(locs, ["http://a/x"])


class SitemapDiscoveryTests(ServerTestCase):
    def test_robots_declared_sitemap_seeds_queue(self):
        s = self.server
        s.add_robots(
            "User-agent: *\nAllow: /\n"
            f"Sitemap: {s.url('/custom-sitemap.xml')}\n")
        s.add("/custom-sitemap.xml",
              urlset_xml(s.url("/p1"), s.url("/p2")),
              content_type="application/xml")
        s.add("/p1", "p1 body")
        s.add("/p2", "p2 body")
        s.add("/seed", "seed body with p1 p2")

        stats = self.crawl(s.url("/seed"), max_pages=10, threads=1)

        self.assertIn("/custom-sitemap.xml", s.paths())
        for probe in main.SITEMAP_COMMON_PATHS:
            self.assertNotIn(probe, s.paths(),
                             f"common probe {probe} should not run when "
                             "robots declares a sitemap")
        self.assertEqual(
            self.indexed_paths(),
            ["/p1", "/p2", "/seed"])
        self.assertEqual(stats["sitemaps_found"], 1)
        self.assertEqual(stats["sitemap_urls"], 2)
        self.assertIn("Found sitemap(s)", self.stdout)
        self.assertIn("Seeding 2 URL(s)", self.stdout)

    def test_multiple_declared_sitemaps_all_parsed(self):
        s = self.server
        s.add_robots(
            "User-agent: *\nAllow: /\n"
            f"Sitemap: {s.url('/sm-a.xml')}\n"
            f"Sitemap: {s.url('/sm-b.xml')}\n")
        s.add("/sm-a.xml",
              urlset_xml(s.url("/p1")),
              content_type="application/xml")
        s.add("/sm-b.xml",
              urlset_xml(s.url("/p2"), s.url("/p3")),
              content_type="application/xml")
        for p in ("/p1", "/p2", "/p3"):
            s.add(p, p.lstrip("/") + " body")
        s.add("/seed", "seed body")

        stats = self.crawl(s.url("/seed"), max_pages=10, threads=1)

        self.assertIn("/sm-a.xml", s.paths())
        self.assertIn("/sm-b.xml", s.paths())
        for probe in main.SITEMAP_COMMON_PATHS:
            self.assertNotIn(probe, s.paths(),
                             f"common probe {probe} should not run when "
                             "robots declares sitemaps")
        self.assertEqual(
            self.indexed_paths(),
            ["/p1", "/p2", "/p3", "/seed"])
        self.assertEqual(stats["sitemaps_found"], 2)
        self.assertEqual(stats["sitemap_urls"], 3)

    def test_fallback_to_common_places_when_no_declaration(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/sitemap.xml",
              urlset_xml(s.url("/p1"), s.url("/p2")),
              content_type="application/xml")
        s.add("/p1", "p1 body")
        s.add("/p2", "p2 body")
        s.add("/seed", "seed body")

        stats = self.crawl(s.url("/seed"), max_pages=10, threads=1)

        self.assertIn("/sitemap.xml", s.paths())
        # Stop at the first success: later common places are not probed.
        self.assertNotIn("/sitemap_index.xml", s.paths())
        self.assertEqual(
            self.indexed_paths(),
            ["/p1", "/p2", "/seed"])
        self.assertEqual(stats["sitemaps_found"], 1)
        self.assertEqual(stats["sitemap_urls"], 2)

    def test_sitemapindex_recurses_into_children(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/sitemap.xml",
              sitemapindex_xml(s.url("/sub/one.xml"), s.url("/sub/two.xml")),
              content_type="application/xml")
        s.add("/sub/one.xml",
              urlset_xml(s.url("/p1"), s.url("/p2")),
              content_type="application/xml")
        s.add("/sub/two.xml",
              urlset_xml(s.url("/p3")),
              content_type="application/xml")
        for p in ("/p1", "/p2", "/p3"):
            s.add(p, p.lstrip("/") + " body")
        s.add("/seed", "seed body")

        stats = self.crawl(s.url("/seed"), max_pages=10, threads=1)

        self.assertIn("/sub/one.xml", s.paths())
        self.assertIn("/sub/two.xml", s.paths())
        self.assertEqual(
            self.indexed_paths(),
            ["/p1", "/p2", "/p3", "/seed"])
        self.assertEqual(stats["sitemaps_found"], 3)  # index + 2 children
        self.assertEqual(stats["sitemap_urls"], 3)

    def test_malformed_sitemap_tries_next_location(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/sitemap.xml", "this is not xml at all",
              content_type="application/xml")
        s.add("/sitemap_index.xml",
              urlset_xml(s.url("/p1")),
              content_type="application/xml")
        s.add("/p1", "p1 body")
        s.add("/seed", "seed body")

        stats = self.crawl(s.url("/seed"), max_pages=10, threads=1)

        self.assertIn("/sitemap_index.xml", s.paths())
        self.assertIn("/p1", self.indexed_paths())
        self.assertEqual(stats["sitemaps_found"], 1)
        self.assertEqual(stats["sitemap_urls"], 1)

    def test_no_sitemap_prints_message_and_continues_html(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/seed", page("Seed", "seed body", ["/a", "/b"]))
        s.add("/a", "a body")
        s.add("/b", "b body")

        stats = self.crawl(s.url("/seed"), max_pages=10, threads=1)

        for probe in main.SITEMAP_COMMON_PATHS:
            self.assertIn(probe, s.paths(), f"probe {probe} expected")
        self.assertEqual(
            self.indexed_paths(),
            ["/a", "/b", "/seed"])
        self.assertEqual(stats["sitemaps_found"], 0)
        self.assertEqual(stats["sitemap_urls"], 0)
        self.assertIn("No sitemap found", self.stdout)
        self.assertIn("continuing with HTML-based discovery", self.stdout)

    def test_scope_filters_sitemap_locs(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/sitemap.xml",
              urlset_xml(s.url("/p1"),
                         f"http://localhost:{s.port}/out"),
              content_type="application/xml")
        s.add("/p1", "p1 body")
        s.add("/seed", "seed body")

        stats = self.crawl(s.url("/seed"), max_pages=10, threads=1)

        self.assertIn("/p1", self.indexed_paths())
        self.assertNotIn("/out", s.paths())  # filtered before requesting
        self.assertEqual(stats["sitemap_urls"], 1)

    def test_urls_deduplicated_across_child_sitemaps(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/sitemap.xml",
              sitemapindex_xml(s.url("/sub/one.xml"), s.url("/sub/two.xml")),
              content_type="application/xml")
        s.add("/sub/one.xml",
              urlset_xml(s.url("/p1"), s.url("/p2")),
              content_type="application/xml")
        s.add("/sub/two.xml",
              urlset_xml(s.url("/p1")),
              content_type="application/xml")
        s.add("/p1", "p1 body")
        s.add("/p2", "p2 body")
        s.add("/seed", "seed body")

        stats = self.crawl(s.url("/seed"), max_pages=10, threads=1)

        self.assertEqual(self.indexed_paths(), ["/p1", "/p2", "/seed"])
        self.assertEqual(stats["sitemap_urls"], 2)  # set-deduplicated
        self.assertEqual(stats["sitemaps_found"], 3)

    def test_only_sitemap_mode_indexes_without_following_links(self):
        s = self.server
        s.add_robots(
            "User-agent: *\nAllow: /\n"
            f"Sitemap: {s.url('/custom-sitemap.xml')}\n")
        s.add("/custom-sitemap.xml",
              urlset_xml(s.url("/p1"), s.url("/p2")),
              content_type="application/xml")
        s.add("/p1", page("P1", "p1 body", ["/extra"]))
        s.add("/p2", page("P2", "p2 body", ["/extra2"]))
        s.add("/extra", "extra body")
        s.add("/extra2", "extra2 body")
        s.add("/seed", "seed body")

        stats = self.crawl(s.url("/seed"), max_pages=10, threads=1,
                           sitemap="only")

        self.assertEqual(self.indexed_paths(), ["/p1", "/p2"])
        self.assertNotIn("/extra", s.paths())
        self.assertNotIn("/extra2", s.paths())
        self.assertNotIn("/seed", s.paths())  # seed not in the sitemap
        self.assertEqual(stats["links_discovered"], 0)
        self.assertEqual(stats["sitemap_urls"], 2)
        self.assertIn("HTML link discovery disabled", self.stdout)

    def test_only_sitemap_aborts_when_no_sitemap(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/seed", page("Seed", "seed body", ["/a"]))
        s.add("/a", "a body")

        stats = self.crawl(s.url("/seed"), max_pages=10, threads=1,
                           sitemap="only")

        self.assertEqual(self.indexed_paths(), [])
        self.assertEqual(stats["indexed"], 0)
        self.assertNotIn("/seed", s.paths())  # nothing crawled after abort
        self.assertIn("No sitemap found", self.stdout)
        self.assertIn("aborting crawl", self.stdout)

    def test_skip_sitemap_makes_no_probes(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/sitemap.xml",
              urlset_xml(s.url("/p1")),
              content_type="application/xml")
        s.add("/p1", "p1 body")
        s.add("/seed", page("Seed", "seed body", ["/a"]))
        s.add("/a", "a body")

        stats = self.crawl(s.url("/seed"), max_pages=10, threads=1,
                           sitemap="skip")

        self.assertNotIn("/sitemap.xml", s.paths())
        self.assertEqual(
            self.indexed_paths(),
            ["/a", "/seed"])
        self.assertEqual(stats["sitemaps_found"], 0)
        self.assertNotIn("No sitemap found", self.stdout)


class SitemapCliTests(ServerTestCase):
    def test_flags_are_mutually_exclusive(self):
        s = self.server
        code, _, err = run_cli(
            ["crawl", s.url("/seed"), "--no-sitemap", "--only-sitemap"])
        self.assertEqual(code, 2)
        self.assertIn("mutually exclusive", err)

    def test_only_sitemap_flag_runs(self):
        s = self.server
        s.add_robots(
            "User-agent: *\nAllow: /\n"
            f"Sitemap: {s.url('/custom-sitemap.xml')}\n")
        s.add("/custom-sitemap.xml",
              urlset_xml(s.url("/p1")),
              content_type="application/xml")
        s.add("/p1", "p1 body")
        s.add("/seed", "seed body")

        code, out, _ = run_cli(
            ["crawl", s.url("/seed"), "--only-sitemap", "--max-pages", "5"])
        self.assertIsNone(code)
        self.assertIn("/p1", self.indexed_paths())
        self.assertIn("Seeding 1 URL(s)", out)

    def test_no_sitemap_flag_runs(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/sitemap.xml",
              urlset_xml(s.url("/p1")),
              content_type="application/xml")
        s.add("/p1", "p1 body")
        s.add("/seed", page("Seed", "seed body", ["/a"]))
        s.add("/a", "a body")

        code, _, _ = run_cli(
            ["crawl", s.url("/seed"), "--no-sitemap", "--max-pages", "5"])
        self.assertIsNone(code)
        self.assertNotIn("/sitemap.xml", s.paths())
        self.assertIn("/a", self.indexed_paths())


if __name__ == "__main__":
    unittest.main()
