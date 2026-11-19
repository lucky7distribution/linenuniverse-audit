#!/usr/bin/env python3
"""Build a dashboard summary from the collected, read-only crawl records."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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
    if end is None:
        raise ValueError(f"Incomplete crawl; previous published data will be preserved: {path}")
    records = rows[1:-1] if end else rows[1:]
    pages = [record for record in records if record.get("record_type") == "url"]
    if end.get("checked_count") != len(pages) or rows[0].get("requested_urls") != len(pages):
        raise ValueError(f"Crawl coverage does not match its declared counts: {path}")
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
    if sitemap_header.get("run_id") != category_header.get("run_id"):
        raise ValueError("Cannot combine sitemap and category data from different runs")
    if any(page.get("error") for page in [*pages, *categories]):
        raise ValueError("Network failures remain unverified; refusing to replace the published report with incomplete evidence")
    run_id = sitemap_header.get("run_id") or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    previous = json.loads(args.output.read_text(encoding="utf-8")) if args.output.exists() else None
    html_responses = [
        page for page in pages if page.get("status") == 200 and "html" in (page.get("content_type") or "").lower()
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
        if page.get("status") == 200 and page.get("redirect_chain")
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
    empty_alt: list[dict[str, str]] = []
    decorative_count = 0
    image_count = 0
    for page in html_pages:
        for image in page.get("images", []):
            image_count += 1
            if not image.get("alt", "").strip():
                if image.get("decorative"):
                    decorative_count += 1
                elif image.get("alt_present", False):
                    empty_alt.append({"page_url": page["url"], "image_url": image.get("src", "")})
                else:
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
                "a canonical on a different hostname. This is a public markup observation; "
                "Google's selected canonical has not been measured."
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
            "Linked category destinations return HTTP errors",
            "Broken links",
            "High",
            10,
            (
                f"{len(broken_category_urls)} currently linked category destinations returned HTTP errors. "
                f"They occur across {len(source_pages)} distinct source URLs among "
                f"{len(html_responses)} successful sitemap HTML responses. Per-destination source counts are listed below."
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
                "Review each duplicate group and use unique descriptions where the pages target "
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
                "Review each affected page template and add a descriptive visible heading "
                "where it improves page clarity."
            ),
            implementation_type="Odoo page/template content",
        ),
        issue(
            "images-missing-alt-text",
            "Image elements are missing the alt attribute",
            "Accessibility & image SEO",
            "Low",
            8,
            (
                f"{len(missing_alt)} image elements have no alt attribute. "
                f"Separately, {len(empty_alt)} explicitly empty alternatives need contextual review; "
                f"{decorative_count} explicitly decorative elements were excluded."
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
    issues.append(issue(
        "images-empty-alt-review", "Empty image alternatives need contextual review", "Accessibility & image SEO", "Low", 6,
        f"{len(empty_alt)} image elements explicitly use empty alt text without an observed decorative marker. This may be correct; manual context review is required.",
        sorted({image["page_url"] for image in empty_alt}),
        "Keep empty alt text for decorative or redundant images; describe meaningful images. Do not fill decorative alt text just to clear a counter.",
        image_count=len(empty_alt), images=empty_alt, finding_status="Needs review",
    ))
    issues = [item for item in issues if item["affected_url_count"] > 0]
    for item in issues:
        item.setdefault("finding_status", "Confirmed")
        item["last_verified_at"] = max(sitemap_end["completed_at"], category_end["completed_at"])

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
            "run_id": run_id,
            "cache_policy": sitemap_header.get("cache_policy", "Not recorded"),
            "source_crawl_files": [args.sitemap_crawl.name, args.category_crawl.name],
            "site": "https://www.linenuniverse.net/",
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "duration_seconds": duration_seconds,
            "scope": "Sitemap crawl plus linked category URLs without query strings or pagination.",
            "sitemap_entries": sitemap_header["sitemap_entries"],
            "unique_sitemap_urls": sitemap_header["unique_sitemap_urls"],
            "sitemap_urls_checked": len(pages),
            "sitemap_successful_urls": sum(page.get("status") == 200 for page in pages),
            "duplicate_sitemap_urls": sitemap_header.get("duplicate_sitemap_urls", 0),
            "linked_category_urls_checked": len(categories),
            "status_counts": dict(status_counts),
            "html_pages": len(html_pages),
            "html_responses": len(html_responses),
            "robots_text": sitemap_header["robots_text"],
            "robots_allows_crawling": not any(page.get("skipped") for page in pages),
            "unverified_urls": [page["url"] for page in [*pages, *categories] if page.get("error") or page.get("status") is None],
            "limitations": [
                f"Coverage is {len(pages)} sitemap URLs plus {len(categories)} current linked non-sitemap category paths; this is not a full link-graph crawl.",
                "Missing alt attributes and explicitly empty alt text are now counted separately; old combined alt counts are not directly comparable.",
                "Cache bypass prevents response reuse in this crawler; application-side generated caches cannot be purged through public HTTP requests.",
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
            "images_empty_alt_review": len(empty_alt),
            "images_explicitly_decorative": decorative_count,
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
    data["comparison"] = compare_runs(previous, data)
    for row in inventory:
        record = next(page for page in [*pages, *categories] if page["url"] == row["url"])
        row["checked_at"] = record.get("checked_at")
        row["response_headers"] = record.get("response_headers", {})
        row["body_sha256"] = record.get("body_sha256")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    if previous:
        previous_id = previous["audit"].get("run_id") or "baseline-" + previous["audit"]["completed_at"][:10]
        archive = args.output.parent / "runs" / previous_id
        archive.mkdir(parents=True, exist_ok=True)
        if not (archive / "dashboard-summary.json").exists():
            (archive / "dashboard-summary.json").write_text(json.dumps(previous, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            prior_inventory = args.output.with_name("url-inventory.json")
            if prior_inventory.exists():
                (archive / "url-inventory.json").write_bytes(prior_inventory.read_bytes())
    run_folder = args.output.parent / "runs" / run_id
    run_folder.mkdir(parents=True, exist_ok=False)
    summary_bytes = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    inventory_bytes = (json.dumps(inventory, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    (run_folder / "dashboard-summary.json").write_bytes(summary_bytes)
    (run_folder / "url-inventory.json").write_bytes(inventory_bytes)
    args.output.write_bytes(summary_bytes)
    inventory_path = args.output.with_name("url-inventory.json")
    inventory_path.write_bytes(inventory_bytes)
    manifest = {
        "run_id": run_id, "completed_at": data["audit"]["completed_at"],
        "summary_url": f"/audit-data/runs/{run_id}/dashboard-summary.json",
        "inventory_url": f"/audit-data/runs/{run_id}/url-inventory.json",
        "summary_sha256": hashlib.sha256(summary_bytes).hexdigest(),
        "inventory_sha256": hashlib.sha256(inventory_bytes).hexdigest(),
    }
    history = []
    for stored in sorted((args.output.parent / "runs").glob("*/dashboard-summary.json")):
        saved = json.loads(stored.read_text(encoding="utf-8"))
        history.append({"run_id": stored.parent.name, "completed_at": saved["audit"]["completed_at"],
                        "summary_url": f"/audit-data/runs/{stored.parent.name}/dashboard-summary.json",
                        "inventory_url": f"/audit-data/runs/{stored.parent.name}/url-inventory.json",
                        "metrics": saved["metrics"]})
    (args.output.parent / "history.json").write_text(json.dumps(sorted(history, key=lambda item: item["completed_at"], reverse=True), indent=2) + "\n", encoding="utf-8")
    pointer = args.output.parent / "latest.json"
    temporary = pointer.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, pointer)
    print(f"Saved dashboard data: {args.output.resolve()}")
    print(f"Saved URL inventory: {inventory_path.resolve()} ({len(inventory)} URLs)")
    print(
        f"URLs checked={len(pages)}; category routes checked={len(categories)}; "
        f"issues={len(issues)}; missing descriptions={len(missing_descriptions)}; "
        f"cross-host canonicals={len(canonical_mismatches)}; "
        f"broken category targets={len(broken_categories)}"
    )


def compare_runs(previous: dict | None, current: dict) -> dict:
    if not previous:
        return {"previous_completed_at": None, "metrics": [], "issues": []}
    comparisons = []
    for key, value in current["metrics"].items():
        old = previous.get("metrics", {}).get(key)
        if isinstance(value, int) and isinstance(old, int):
            comparisons.append({"metric": key, "previous": old, "current": value, "change": value - old,
                                "comparable": key != "images_missing_alt"})
    before = {item["id"]: item for item in previous.get("issues", [])}
    after = {item["id"]: item for item in current.get("issues", [])}
    issue_rows = []
    for key in sorted(before.keys() | after.keys()):
        old = set(before.get(key, {}).get("affected_urls", []))
        new = set(after.get(key, {}).get("affected_urls", []))
        issue_rows.append({"id": key, "persisting": sorted(old & new), "newly_detected": sorted(new - old),
                           "no_longer_detected": sorted(old - new),
                           "note": "No longer detected is not proof of a fixed destination: a stale link may have been removed or replaced. Compare source evidence. Coverage and rule changes also affect comparisons."})
    return {"previous_completed_at": previous["audit"]["completed_at"], "metrics": comparisons, "issues": issue_rows}


if __name__ == "__main__":
    main()
