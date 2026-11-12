#!/usr/bin/env python3
"""Check linked, non-sitemap Linen Universe category URLs at a low request rate."""

from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.robotparser
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from crawl_sitemap import USER_AGENT, page_record


def main() -> None:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("crawl_file", type=Path, help="JSONL file from crawl_sitemap.py")
    cli.add_argument("--delay", type=float, default=1.0, help="Seconds between page requests")
    cli.add_argument("--limit", type=int, help="Optional maximum number of category URLs to check")
    cli.add_argument("--output", type=Path, help="JSONL output path")
    args = cli.parse_args()
    if args.delay < 0.5:
        cli.error("--delay must be at least 0.5 seconds to keep the crawl conservative")

    with args.crawl_file.open(encoding="utf-8") as stream:
        records = [json.loads(line) for line in stream if line.strip()]
    header = records[0]
    pages = [record for record in records[1:] if record.get("record_type") == "url"]
    sitemap_urls = {page["url"] for page in pages}
    sources: dict[str, set[str]] = {}
    for page in pages:
        for target in page.get("internal_links", []):
            parts = urllib.parse.urlsplit(target)
            if (
                parts.hostname == "www.linenuniverse.net"
                and parts.path.startswith("/shop/category/")
                and not parts.query
                and "/page/" not in parts.path
                and target not in sitemap_urls
            ):
                sources.setdefault(target, set()).add(page["url"])

    urls = sorted(sources)
    if args.limit is not None:
        urls = urls[: args.limit]
    output = args.output or Path("audit-data") / (
        "linked-category-crawl-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".jsonl"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite existing crawl data: {output}")

    robots = urllib.robotparser.RobotFileParser()
    robots.set_url(header["robots_url"])
    robots.parse(header["robots_text"].splitlines())
    status_counts: Counter[str] = Counter()
    run_header = {
        "record_type": "crawl",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "source_crawl_file": str(args.crawl_file),
        "requested_urls": len(urls),
        "delay_seconds": args.delay,
        "scope": "Linked category URLs absent from sitemap; query and paginated routes excluded.",
        "user_agent": USER_AGENT,
    }

    with output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(run_header, ensure_ascii=False) + "\n")
        for index, url in enumerate(urls, start=1):
            source_pages = sorted(sources[url])
            if not robots.can_fetch(USER_AGENT, url):
                result: dict[str, object] = {
                    "record_type": "url",
                    "url": url,
                    "status": None,
                    "skipped": "Disallowed by robots.txt",
                }
            else:
                result = page_record(
                    {
                        "url": url,
                        "lastmod": None,
                        "source_sitemap": None,
                    }
                )
            result.update(
                {
                    "source": "internal_link",
                    "source_page_count": len(source_pages),
                    "source_pages": source_pages,
                }
            )
            status_counts[str(result.get("status", "not_checked"))] += 1
            stream.write(json.dumps(result, ensure_ascii=False) + "\n")
            stream.flush()
            if index == 1 or index % 10 == 0 or index == len(urls):
                print(f"{index}/{len(urls)} checked; latest={url}", flush=True)
            if index < len(urls):
                time.sleep(args.delay)

        stream.write(
            json.dumps(
                {
                    "record_type": "crawl_end",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "checked_count": len(urls),
                    "status_counts": dict(status_counts),
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        stream.flush()

    print(f"Saved linked-category crawl: {output.resolve()}")


if __name__ == "__main__":
    main()
