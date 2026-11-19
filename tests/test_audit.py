import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import crawl_sitemap as crawler
import build_dashboard_data as builder


class CrawlContracts(unittest.TestCase):
    def test_nonce_removed_but_product_variant_preserved(self):
        url = "https://www.linenuniverse.net/shop/a?attribute_values=624&_linen_audit=random#top"
        self.assertEqual(crawler.clean_audit_url(url), "https://www.linenuniverse.net/shop/a?attribute_values=624")

    def test_target_scope_rejects_external_or_private_requests(self):
        for url in ("http://www.linenuniverse.net/", "https://127.0.0.1/", "https://www.linenuniverse.net.attacker.test/", "https://www.linenuniverse.net:8443/", "https://user@www.linenuniverse.net/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                crawler.validate_url(url)

    def test_alt_markup_distinguishes_missing_empty_and_decorative(self):
        p = crawler.PageParser()
        p.feed('<img src="a"><img src="b" alt=""><img src="c" alt="" role="presentation"><img src="d" alt="White towels">')
        self.assertEqual([x["alt_present"] for x in p.images], [False, True, True, True])
        self.assertEqual([x["decorative"] for x in p.images], [False, False, True, False])


class ReportContracts(unittest.TestCase):
    def make_crawl(self, path, pages, run="test-run", complete=True):
        header = {"record_type": "crawl", "run_id": run, "started_at": "2026-11-19T10:00:00+00:00", "requested_urls": len(pages), "sitemap_entries": len(pages), "unique_sitemap_urls": len(pages), "robots_text": "User-agent: *\nAllow: /", "cache_policy": "test"}
        rows = [header, *pages]
        if complete:
            rows.append({"record_type": "crawl_end", "completed_at": "2026-11-19T10:01:00+00:00", "checked_count": len(pages)})
        path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    def page(self, url, **extras):
        return {"record_type": "url", "url": url, "final_url": url, "status": 200, "content_type": "text/html", "title": "Unique", "meta_description": "A description", "headings": {"h1": ["Heading"]}, "canonical": [url], "source": "sitemap", **extras}

    def build(self, sitemap, categories, output):
        with patch.object(sys, "argv", ["build", str(sitemap), str(categories), "--output", str(output)]):
            builder.main()

    def test_new_complete_run_replaces_counts_and_archives_old_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); sm = root / "sm.jsonl"; cat = root / "cat.jsonl"; out = root / "dashboard-summary.json"
            home = self.page("https://www.linenuniverse.net/", images=[{"src": "a", "alt": "", "alt_present": False}, {"src": "b", "alt": "", "alt_present": True}, {"src": "c", "alt": "", "decorative": True}])
            target = "https://www.linenuniverse.net/shop/category/old-1"
            self.make_crawl(sm, [home])
            self.make_crawl(cat, [self.page(target, status=404, source="internal_link", source_pages=[home["url"]], source_page_count=1)])
            prior = {"audit": {"completed_at": "2026-11-12T10:00:00+00:00"}, "metrics": {"missing_meta_descriptions": 5, "images_missing_alt": 27}, "issues": [{"id": "missing-meta-descriptions", "affected_urls": [home["url"]]}]}
            out.write_text(json.dumps(prior), encoding="utf-8"); (root / "url-inventory.json").write_text("[]")
            self.build(sm, cat, out)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(report["metrics"]["missing_meta_descriptions"], 0)
            self.assertNotIn("missing-meta-descriptions", [item["id"] for item in report["issues"]])
            self.assertEqual(report["metrics"]["images_missing_alt"], 1)
            self.assertEqual(report["metrics"]["images_empty_alt_review"], 1)
            self.assertEqual(report["metrics"]["images_explicitly_decorative"], 1)
            self.assertEqual(report["metrics"]["broken_category_destinations"], 1)
            self.assertTrue((root / "runs/baseline-2026-11-12/dashboard-summary.json").exists())
            manifest = json.loads((root / "latest.json").read_text())
            self.assertEqual(manifest["run_id"], "test-run")
            self.assertEqual(len(json.loads((root / "history.json").read_text())), 2)
            item = next(x for x in report["comparison"]["issues"] if x["id"] == "missing-meta-descriptions")
            self.assertEqual(item["no_longer_detected"], [home["url"]])

    def test_incomplete_crawl_does_not_replace_previous_report(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); sm = root / "sm.jsonl"; cat = root / "cat.jsonl"; out = root / "dashboard-summary.json"
            self.make_crawl(sm, [], complete=False); self.make_crawl(cat, [])
            out.write_text('{"unchanged":true}')
            with self.assertRaisesRegex(ValueError, "Incomplete"):
                self.build(sm, cat, out)
            self.assertEqual(out.read_text(), '{"unchanged":true}')

    def test_mixed_runs_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); sm = root / "sm.jsonl"; cat = root / "cat.jsonl"
            self.make_crawl(sm, [], run="a"); self.make_crawl(cat, [], run="b")
            with self.assertRaisesRegex(ValueError, "different runs"):
                self.build(sm, cat, root / "dashboard-summary.json")

    def test_removed_broken_link_is_not_called_a_fixed_destination(self):
        old = {"audit": {"completed_at": "old"}, "metrics": {}, "issues": [{"id": "broken-category-navigation", "affected_urls": ["https://www.linenuniverse.net/old"]}]}
        result = builder.compare_runs(old, {"metrics": {}, "issues": []})
        self.assertEqual(len(result["issues"][0]["no_longer_detected"]), 1)
        self.assertIn("not proof", result["issues"][0]["note"])


if __name__ == "__main__":
    unittest.main()
