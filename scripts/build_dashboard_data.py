#!/usr/bin/env python3
"""Build a dashboard summary from the collected, read-only crawl records."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


def load_jsonl(path: Path) -> tuple[dict, list[dict], dict | None]:
    with path.open(encoding="utf-8") as stream:
        rows = [json.loads(line) for line in stream if line.strip()]
    if not rows or rows[0].get("record_type") != "crawl":
        raise ValueError(f"Missing crawl header in {path}")
    end = rows[-1] if rows[-1].get("record_type") == "crawl_end" else None
    records = rows[1:-1] if end else rows[1:]
    pages = [record for record in records if record.get("record_type") == "url"]
    return rows[0], pages, end


def grouped_values(pages: list[dict], field: str) -> list[dict]:
    groups: dict[str, list[str]] = defaultdict(list)
    original: dict[str, str] = {}
    for page in pages:
        value = page.get(field)
        if isinstance(value, str) and value.strip():
            key = value.strip().casefold()
            groups[key].append(page["url"])
            original[key] = value.strip()
    return [
        {"value": original[key], "urls": sorted(urls)}
        for key, urls in groups.items()
        if len(urls) > 1
    ]


def issue(
    issue_id: str,
    title: str,
    category: str,
    severity: str,
    confidence: int,
    evidence: str,
    affected_urls: list[str],
    recommended_fix: str,
    **extra: object,
) -> dict:
    return {
        "id": issue_id,
        "title": title,
        "category": category,
        "severity": severity,
        "confidence": confidence,
        "evidence": evidence,
        "affected_url_count": len(affected_urls),
        "affected_urls": affected_urls,
        "recommended_fix": recommended_fix,
        **extra,
    }


def page_type(page: dict) -> str:
    path = urlsplit(page["url"]).path.rstrip("/")
    if path == "":
        return "Homepage"
    if "atom+xml" in (page.get("content_type") or "").lower():
        return "Feed"
    if path.startswith("/shop/category/"):
        return "Category"
    if path.startswith("/shop/"):
        return "Product"
    if path.startswith("/blog"):
        return "Blog"
    return "Other"


def main() -> None:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("sitemap_crawl", type=Path)
    cli.add_argument("category_crawl", type=Path)
    cli.add_argument("--output", type=Path, default=Path("audit-data/dashboard-summary.json"))
    args = cli.parse_args()

    sitemap_header, pages, sitemap_end = load_jsonl(args.sitemap_crawl)
    category_header, categories, category_end = load_jsonl(args.category_crawl)
    html_responses = [
        page for page in pages if "html" in (page.get("content_type") or "").lower()
    ]
    html_pages = [
        page
        for page in html_responses
        if page.get("url") == page.get("final_url")
    ]
    sitemap_urls = [page["url"] for page in pages]
    canonical_mismatches = [
        page
        for page in html_pages
        if page.get("canonical")
        and any(
            urlsplit(canonical).netloc.lower() != urlsplit(page["url"]).netloc.lower()
            for canonical in page["canonical"]
        )
    ]
    missing_descriptions = [
        page["url"] for page in html_pages if not (page.get("meta_description") or "").strip()
    ]
    duplicate_titles = grouped_values(html_pages, "title")
    duplicate_descriptions = grouped_values(html_pages, "meta_description")
    broken_categories = [
        page for page in categories if isinstance(page.get("status"), int) and page["status"] >= 400
    ]
    redirected_categories = [
        page
        for page in categories
        if page.get("final_url") and page["final_url"] != page.get("url")
    ]
    redirected_sitemap_urls = [
        page
        for page in pages
        if page.get("final_url") and page["final_url"] != page.get("url")
    ]
    missing_h1 = [
        page["url"]
        for page in html_pages
        if not any(
            isinstance(value, str) and value.strip()
            for value in page.get("headings", {}).get("h1", [])
        )
    ]

    missing_alt: list[dict[str, str]] = []
    image_count = 0
    for page in html_pages:
        for image in page.get("images", []):
            image_count += 1
            if not image.get("alt", "").strip():
                missing_alt.append({"page_url": page["url"], "image_url": image.get("src", "")})

    sitemap_host_counts: dict[str, int] = defaultdict(int)
    for page in pages:
        for canonical in page.get("canonical", []):
            sitemap_host_counts[urlsplit(canonical).netloc] += 1

    title_url_count = sum(len(group["urls"]) for group in duplicate_titles)
    description_url_count = sum(len(group["urls"]) for group in duplicate_descriptions)
    broken_category_urls = sorted(page["url"] for page in broken_categories)
    source_pages = sorted(
        {
            source
            for page in broken_categories
            for source in page.get("source_pages", [])
        }
    )

    issues = [
        issue(
            "legacy-canonical-host",
            "Canonical tags point to the legacy Odoo hostname",
            "Indexability & canonicals",
            "High",
            9,
            (
                f"{len(canonical_mismatches)} of {len(html_pages)} direct sitemap HTML pages declare "
                "a canonical on linenuniverse.odoo.com. Three representative canonical targets "
                "were independently checked and returned HTTP 200 with self-canonicals."
            ),
            sorted(page["url"] for page in canonical_mismatches),
            (
                "Review the Odoo website-domain/canonical configuration and make "
                "www.linenuniverse.net the preferred canonical host. Verify redirects and "
                "canonical tags on representative page templates before applying sitewide."
            ),
            canonical_host_counts=dict(sitemap_host_counts),
            implementation_type="Odoo website/domain configuration or shared template",
        ),
        issue(
            "broken-category-navigation",
            "Linked category destinations return 404",
            "Broken links",
            "High",
            10,
            (
                f"{len(broken_category_urls)} linked category destinations returned HTTP 404 "
                f"when rechecked. Each destination was linked from {len(source_pages)} of "
                f"{len(html_pages)} crawled HTML source pages."
            ),
            broken_category_urls,
            (
                "Update or remove the stale category destinations in the shared Odoo "
                "navigation/category menu; redirect only where a clear replacement exists. "
                "Retest every source template after the change."
            ),
            source_page_count=len(source_pages),
            source_page_samples=source_pages[:10],
            destinations=[
                {
                    "url": page["url"],
                    "status": page.get("status"),
                    "source_page_count": page.get("source_page_count", 0),
                    "source_page_samples": page.get("source_pages", [])[:10],
                }
                for page in broken_categories
            ],
            implementation_type="Shared Odoo navigation/category configuration",
        ),
        issue(
            "linked-category-redirects",
            "Linked category paths redirect to different URLs",
            "Redirects",
            "Medium",
            9,
            (
                f"{len(redirected_categories)} of {len(categories)} linked category paths "
                "redirected before reaching a successful page."
            ),
            sorted(page["url"] for page in redirected_categories),
            (
                "Update shared Odoo menu and category links to use the final destination URLs "
                "and avoid unnecessary redirects."
            ),
            redirects=[
                {
                    "url": page["url"],
                    "final_url": page.get("final_url"),
                    "redirect_chain": page.get("redirect_chain", []),
                    "source_page_count": page.get("source_page_count", 0),
                }
                for page in redirected_categories
            ],
            implementation_type="Shared Odoo navigation/category configuration",
        ),
        issue(
            "missing-meta-descriptions",
            "HTML pages are missing meta descriptions",
            "On-page SEO",
            "Medium",
            10,
            f"{len(missing_descriptions)} of {len(html_pages)} sitemap HTML pages have no meta description.",
            sorted(missing_descriptions),
            (
                "Add unique, useful descriptions to important product/category pages. "
                "Use Odoo product/category SEO fields and template defaults where appropriate."
            ),
            implementation_type="Odoo SEO fields and product/category content",
        ),
        issue(
            "duplicate-page-titles",
            "Duplicate title tags",
            "On-page SEO",
            "Medium",
            8,
            (
                f"{len(duplicate_titles)} exact duplicate-title groups affect "
                f"{title_url_count} URLs. Review groups for genuinely distinct products; "
                "similar variants may have legitimate overlap."
            ),
            sorted({url for group in duplicate_titles for url in group["urls"]}),
            (
                "Review each duplicate group and make titles specific to the product "
                "attributes that distinguish the pages."
            ),
            groups=duplicate_titles,
            implementation_type="Odoo product SEO fields/content",
        ),
        issue(
            "duplicate-meta-descriptions",
            "Duplicate meta descriptions across product pages",
            "On-page SEO",
            "Medium",
            8,
            (
                f"{len(duplicate_descriptions)} exact duplicate-description groups affect "
                f"{description_url_count} URLs."
            ),
            sorted({url for group in duplicate_descriptions for url in group["urls"]}),
            (
                "Review each duplicate group and use unique titles where the pages target "
                "distinct products or search intents."
            ),
            groups=duplicate_descriptions,
            implementation_type="Odoo product SEO fields/content",
        ),
        issue(
            "sitemap-redirect-urls",
            "Sitemap contains URLs that redirect",
            "Sitemap & crawlability",
            "Low",
            10,
            f"{len(redirected_sitemap_urls)} sitemap entries redirect to other URLs.",
            sorted(page["url"] for page in redirected_sitemap_urls),
            "Replace redirected sitemap entries with their final canonical destinations.",
            redirects=[
                {
                    "url": page["url"],
                    "final_url": page.get("final_url"),
                    "redirect_chain": page.get("redirect_chain", []),
                }
                for page in redirected_sitemap_urls
            ],
            implementation_type="Odoo sitemap/content configuration",
        ),
        issue(
            "missing-h1",
            "HTML pages have no non-empty H1 heading",
            "On-page SEO",
            "Low",
            9,
            f"{len(missing_h1)} sitemap HTML pages have no non-empty H1.",
            sorted(missing_h1),
            (
                "Review the appointment page template and add a descriptive visible heading "
                "where it improves page clarity."
            ),
            implementation_type="Odoo page/template content",
        ),
        issue(
            "images-missing-alt-text",
            "Image elements have missing or empty alt text",
            "Accessibility & image SEO",
            "Low",
            8,
            (
                f"{len(missing_alt)} image elements have no non-empty alt text. "
                "Decorative images may correctly use empty alt text, so review context "
                "before changing them."
            ),
            sorted({image["page_url"] for image in missing_alt}),
            (
                "Add concise descriptive alt text to meaningful product/content images; "
                "keep empty alt text for purely decorative images."
            ),
            image_count=len(missing_alt),
            images=missing_alt,
            implementation_type="Odoo product/content image administration",
        ),
    ]

    started_values = [
        datetime.fromisoformat(value)
        for value in (sitemap_header.get("started_at"), category_header.get("started_at"))
        if value
    ]
    crawl_files = [
        (sitemap_header, args.sitemap_crawl),
        (category_header, args.category_crawl),
    ]
    crawl_ends = [sitemap_end, category_end]
    completed_values = [
        datetime.fromisoformat(end["completed_at"])
        if end and end.get("completed_at")
        else datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        for end, (_, path) in zip(crawl_ends, crawl_files)
    ]
    started_at = min(started_values)
    completed_at = max(completed_values)
    duration_seconds = sum(
        max(
            0,
            int(
                (
                    (
                        datetime.fromisoformat(end["completed_at"])
                        if end and end.get("completed_at")
                        else datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                    )
                    - datetime.fromisoformat(crawl_header["started_at"])
                ).total_seconds()
            ),
        )
        for (crawl_header, path), end in zip(crawl_files, crawl_ends)
    )
    status_counts: dict[str, int] = defaultdict(int)
    for page in pages:
        status_counts[str(page.get("status", "not_checked"))] += 1

    inbound_counts: dict[str, int] = defaultdict(int)
    for page in pages:
        for target in page.get("internal_links", []):
            inbound_counts[target] += 1
    inventory = []
    for page in [*pages, *categories]:
        robots_directives = [
            *(page.get("meta_robots") or []),
            page.get("x_robots_tag") or "",
        ]
        canonical = page.get("canonical") or []
        if not isinstance(canonical, list):
            canonical = [canonical]
        if page.get("status") != 200:
            indexability = "Not assessed (HTTP error)"
        elif any("noindex" in value.lower() for value in robots_directives):
            indexability = "Noindex directive"
        elif canonical and any(
            urlsplit(value).netloc.lower() != urlsplit(page["url"]).netloc.lower()
            for value in canonical
        ):
            indexability = "Canonical points off-host"
        else:
            indexability = "No noindex observed"
        inventory.append(
            {
                "url": page["url"],
                "source": page.get("source", "sitemap"),
                "page_type": page_type(page),
                "status": page.get("status"),
                "final_url": page.get("final_url"),
                "redirect_chain": page.get("redirect_chain", []),
                "indexability": indexability,
                "canonical": canonical[0] if canonical else "",
                "title": page.get("title", ""),
                "meta_description": page.get("meta_description", ""),
                "h1": (page.get("headings") or {}).get("h1", []),
                "inbound_internal_links": max(
                    inbound_counts.get(page["url"], 0),
                    page.get("source_page_count", 0),
                ),
                "outbound_internal_links": len(page.get("internal_links", [])),
                "outbound_external_links": len(page.get("external_links", [])),
                "image_count": len(page.get("images", [])),
                "source_page_count": page.get("source_page_count", 0),
                "lastmod": page.get("lastmod"),
            }
        )

    data = {
        "audit": {
            "site": "https://www.linenuniverse.net/",
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "duration_seconds": duration_seconds,
            "scope": "Sitemap crawl plus linked category URLs without query strings or pagination.",
            "sitemap_entries": sitemap_header["sitemap_entries"],
            "unique_sitemap_urls": sitemap_header["unique_sitemap_urls"],
            "sitemap_urls_checked": len(pages),
            "linked_category_urls_checked": len(categories),
            "status_counts": dict(status_counts),
            "html_pages": len(html_pages),
            "html_responses": len(html_responses),
            "robots_text": sitemap_header["robots_text"],
            "robots_allows_crawling": True,
            "limitations": [
                "Coverage is sitemap URLs plus 42 linked category paths; this is not yet a full link-graph crawl.",
                "Query-string and paginated category URLs were excluded from the linked-category pass.",
                "External links, performance/Core Web Vitals, rendered/mobile behavior, structured data validation, and authenticated Odoo settings have not yet been audited.",
                "Google indexation, traffic, rankings, and revenue data were not available.",
            ],
        },
        "metrics": {
            "sitemap_urls": len(pages),
            "html_pages": len(html_pages),
            "sitemap_http_errors": sum(
                isinstance(page.get("status"), int) and page["status"] >= 400 for page in pages
            ),
            "cross_host_canonicals": len(canonical_mismatches),
            "missing_meta_descriptions": len(missing_descriptions),
            "duplicate_title_groups": len(duplicate_titles),
            "duplicate_title_urls": title_url_count,
            "duplicate_description_groups": len(duplicate_descriptions),
            "duplicate_description_urls": description_url_count,
            "broken_category_destinations": len(broken_categories),
            "category_redirects": len(redirected_categories),
            "sitemap_redirects": len(redirected_sitemap_urls),
            "missing_h1_pages": len(missing_h1),
            "images_checked": image_count,
            "images_missing_alt": len(missing_alt),
            "sitemap_entries_missing_lastmod": sum(not page.get("lastmod") for page in pages),
        },
        "issues": issues,
        "unscored_assessments": [
            "Performance and Core Web Vitals",
            "Structured data validity",
            "External link status",
            "Rendered/mobile functionality",
            "Google Search Console/indexation",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    inventory_path = args.output.with_name("url-inventory.json")
    inventory_path.write_text(
        json.dumps(inventory, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(f"Saved dashboard data: {args.output.resolve()}")
    print(f"Saved URL inventory: {inventory_path.resolve()} ({len(inventory)} URLs)")
    print(
        f"URLs checked={len(pages)}; category routes checked={len(categories)}; "
        f"issues={len(issues)}; missing descriptions={len(missing_descriptions)}; "
        f"cross-host canonicals={len(canonical_mismatches)}; "
        f"broken category targets={len(broken_categories)}"
    )


if __name__ == "__main__":
    main()
