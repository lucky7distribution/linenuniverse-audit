#!/usr/bin/env python3
"""Read-only, rate-limited sitemap crawler for Linen Universe."""

from __future__ import annotations

import argparse
import hashlib
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.parse
import urllib.robotparser
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

SITE_ROOT = "https://www.linenuniverse.net/"
SITEMAP_URL = urllib.parse.urljoin(SITE_ROOT, "sitemap.xml")
ROBOTS_URL = urllib.parse.urljoin(SITE_ROOT, "robots.txt")
USER_AGENT = "LinenUniverseAudit/1.0 (read-only public-site crawl)"
NAMESPACE = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
ALLOWED_HOSTS = {"www.linenuniverse.net", "linenuniverse.net"}
CACHE_KEY = "_linen_audit"
RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
_request_lock = threading.Lock()
_last_request = 0.0
REQUEST_DELAY = 0.5


def validate_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS or parts.username or parts.password or parts.port not in (None, 443):
        raise ValueError(f"Out-of-scope crawl URL: {url}")


def clean_audit_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.urlencode([(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if k != CACHE_KEY])
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def reserve_request() -> None:
    global _last_request
    with _request_lock:
        wait = REQUEST_DELAY - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.description: str | None = None
        self.canonicals: list[str] = []
        self.robots: list[str] = []
        self.headings: dict[str, list[str]] = {f"h{n}": [] for n in range(1, 7)}
        self.links: list[dict[str, str]] = []
        self.images: list[dict[str, str]] = []
        self._in_title = False
        self._heading: str | None = None
        self._heading_parts: list[str] = []
        self._current_link: int | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "title":
            self._in_title = True
        if tag in self.headings:
            self._heading = tag
            self._heading_parts = []
        if tag == "meta":
            if values.get("name", "").lower() == "description":
                self.description = values.get("content")
            elif values.get("name", "").lower() == "robots":
                self.robots.append(values.get("content", ""))
        elif tag == "link" and "canonical" in values.get("rel", "").lower().split():
            self.canonicals.append(values.get("href", ""))
        elif tag == "a" and values.get("href"):
            self.links.append({"href": values["href"] or "", "text": ""})
            self._current_link = len(self.links) - 1
        elif tag == "img":
            self.images.append(
                {
                    "src": values.get("src", "") or "",
                    "alt": values.get("alt", "") or "",
                    "alt_present": "alt" in values,
                    "decorative": (values.get("role") in {"presentation", "none"} or values.get("aria-hidden", "").lower() == "true" or "o_decorative_image" in (values.get("class") or "").split()),
                    "width": values.get("width", "") or "",
                    "height": values.get("height", "") or "",
                }
            )

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag == "a":
            self._current_link = None
        if tag == self._heading:
            text = " ".join(" ".join(self._heading_parts).split())
            self.headings[tag].append(text)
            self._heading = None
            self._heading_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._heading:
            self._heading_parts.append(data)
        if self._current_link is not None and data.strip():
            link = self.links[self._current_link]
            link["text"] = f'{link["text"]} {data.strip()}'.strip()


class RedirectRecorder(urllib.request.HTTPRedirectHandler):
    def __init__(self) -> None:
        super().__init__()
        self.chain: list[str] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(urllib.parse.urljoin(req.full_url, newurl))
        self.chain.append(clean_audit_url(urllib.parse.urljoin(req.full_url, newurl)))
        reserve_request()
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, timeout: int = 25) -> tuple[int, str, object, bytes, list[str]]:
    validate_url(url)
    for attempt in range(2):
        recorder = RedirectRecorder()
        opener = urllib.request.build_opener(recorder)
        parts = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        query.append((CACHE_KEY, RUN_ID + "-" + uuid.uuid4().hex[:8]))
        request_url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), ""))
        request = urllib.request.Request(request_url, headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache, no-store, max-age=0", "Pragma": "no-cache"})
        reserve_request()
        try:
            try:
                response = opener.open(request, timeout=timeout)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                result = (response.code, clean_audit_url(response.geturl()), response.headers, response.read(), recorder.chain)
            if attempt == 0 and (result[0] in {404, 408, 429} or result[0] >= 500):
                retry_after = result[2].get("Retry-After", "2")
                time.sleep(min(30, max(2, int(retry_after) if retry_after.isdigit() else 2)))
                continue
            result[2]["X-Linen-Audit-Attempts"] = str(attempt + 1)
            result[2]["X-Linen-Audit-Request-URL"] = request_url
            return result
        except (urllib.error.URLError, TimeoutError):
            if attempt:
                raise
            time.sleep(2)
    raise RuntimeError("Fetch did not produce a response")


def read_sitemap(url: str, seen: set[str]) -> list[dict[str, str | None]]:
    if url in seen:
        raise ValueError(f"Repeated or cyclic sitemap reference: {url}")
    seen.add(url)
    status, final_url, _, body, _ = fetch(url)
    if status != 200:
        raise RuntimeError(f"Sitemap returned HTTP {status}: {final_url}")
    root = ET.fromstring(body)
    records: list[dict[str, str | None]] = []
    if root.tag.endswith("sitemapindex"):
        for sitemap in root.findall(f"{NAMESPACE}sitemap"):
            loc = sitemap.findtext(f"{NAMESPACE}loc")
            if loc:
                records.extend(read_sitemap(loc.strip(), seen))
    elif root.tag.endswith("urlset"):
        for entry in root.findall(f"{NAMESPACE}url"):
            loc = entry.findtext(f"{NAMESPACE}loc")
            if loc:
                records.append(
                    {
                        "url": loc.strip(),
                        "lastmod": entry.findtext(f"{NAMESPACE}lastmod"),
                        "source_sitemap": final_url,
                    }
                )
    else:
        raise ValueError(f"Unexpected sitemap XML root: {root.tag}")
    return records


def page_record(entry: dict[str, str | None]) -> dict[str, object]:
    url = entry["url"]
    assert url is not None
    started = datetime.now(timezone.utc).isoformat()
    record: dict[str, object] = {
        "record_type": "url",
        "source": "sitemap",
        **entry,
        "checked_at": started,
    }
    try:
        status, final_url, headers, body, redirects = fetch(url)
        record.update(
            {
                "status": status,
                "final_url": final_url,
                "redirect_chain": redirects,
                "content_type": headers.get("Content-Type"),
                "x_robots_tag": headers.get("X-Robots-Tag"),
                "bytes": len(body),
                "body_sha256": hashlib.sha256(body).hexdigest(),
                "request_url": headers.get("X-Linen-Audit-Request-URL"),
                "attempts": int(headers.get("X-Linen-Audit-Attempts", "1")),
                "response_headers": {name: headers.get(name) for name in ("Date", "Age", "Cache-Control", "ETag", "Last-Modified", "X-Cache", "X-Vercel-Cache") if headers.get(name) is not None},
            }
        )
        if "html" in (headers.get("Content-Type") or "").lower():
            charset = headers.get_content_charset() or "utf-8"
            parser = PageParser()
            parser.feed(body.decode(charset, errors="replace"))
            page_url = urllib.parse.urlsplit(final_url)
            internal_links: list[str] = []
            external_links: list[str] = []
            for link in parser.links:
                target = urllib.parse.urljoin(final_url, link["href"])
                parts = urllib.parse.urlsplit(target)
                normalized = urllib.parse.urlunsplit(
                    (parts.scheme, parts.netloc, parts.path, parts.query, "")
                )
                if parts.hostname and parts.hostname.lower() == (page_url.hostname or "").lower():
                    internal_links.append(normalized)
                elif parts.scheme in {"http", "https"}:
                    external_links.append(normalized)
            record.update(
                {
                    "title": " ".join(" ".join(parser.title_parts).split()),
                    "meta_description": parser.description,
                    "canonical": parser.canonicals,
                    "meta_robots": parser.robots,
                    "headings": parser.headings,
                    "internal_links": internal_links,
                    "external_links": external_links,
                    "images": parser.images,
                }
            )
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
        record["error"] = f"{type(error).__name__}: {error}"
    return record


def main() -> None:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--delay", type=float, default=1.0, help="Seconds between page requests")
    cli.add_argument("--limit", type=int, help="Optional maximum number of sitemap URLs to check")
    cli.add_argument("--output", type=Path, help="JSONL output path")
    cli.add_argument("--workers", type=int, default=3, choices=range(1, 5), help="Concurrent readers; all request starts share the delay limit")
    args = cli.parse_args()
    if args.delay < 0.5:
        cli.error("--delay must be at least 0.5 seconds to keep the crawl conservative")
    global REQUEST_DELAY
    REQUEST_DELAY = args.delay

    status, _, _, robots_body, _ = fetch(ROBOTS_URL)
    if status != 200:
        raise RuntimeError(f"robots.txt returned HTTP {status}")
    robots = urllib.robotparser.RobotFileParser()
    robots.set_url(ROBOTS_URL)
    robots.parse(robots_body.decode("utf-8", errors="replace").splitlines())

    entries = read_sitemap(SITEMAP_URL, set())
    unique_entries: dict[str, dict[str, str | None]] = {}
    for entry in entries:
        url = entry["url"]
        if url:
            unique_entries.setdefault(url, entry)
    ordered_entries = list(unique_entries.values())
    if args.limit is not None:
        ordered_entries = ordered_entries[: args.limit]

    output = args.output or Path("audit-data") / (
        "sitemap-crawl-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".jsonl"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite existing crawl data: {output}")

    header = {
        "record_type": "crawl",
        "run_id": RUN_ID,
        "cache_policy": "Unique per-request query nonce, no-cache/no-store headers, no local response reuse; response cache headers and SHA-256 recorded. Origin-generated caches cannot be forcibly purged by this public crawler.",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "site_root": SITE_ROOT,
        "robots_url": ROBOTS_URL,
        "robots_text": robots_body.decode("utf-8", errors="replace"),
        "sitemap_url": SITEMAP_URL,
        "sitemap_entries": len(entries),
        "unique_sitemap_urls": len(unique_entries),
        "duplicate_sitemap_urls": len(entries) - len(unique_entries),
        "requested_urls": len(ordered_entries),
        "user_agent": USER_AGENT,
        "delay_seconds": args.delay,
        "limitations": ["Sitemap URLs only; navigation-only discovery is not included in this run."],
    }

    errors = 0
    status_counts: Counter[str] = Counter()
    def check(entry):
        if not robots.can_fetch(USER_AGENT, entry["url"] or ""):
            return {"record_type": "url", "source": "sitemap", **entry, "checked_at": datetime.now(timezone.utc).isoformat(), "status": None, "skipped": "Disallowed by robots.txt"}
        return page_record(entry)

    with output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(header, ensure_ascii=False) + "\n")
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            for index, result in enumerate(executor.map(check, ordered_entries), start=1):
                url = result["url"]
                if result.get("error"):
                    errors += 1
                status_counts[str(result.get("status", "not_checked"))] += 1
                stream.write(json.dumps(result, ensure_ascii=False) + "\n")
                stream.flush()
                if index == 1 or index % 20 == 0 or index == len(ordered_entries):
                    print(
                        f"{index}/{len(ordered_entries)} checked; "
                        f"errors={errors}; latest={url}",
                        flush=True,
                    )

        stream.write(
            json.dumps(
                {
                    "record_type": "crawl_end",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "checked_count": len(ordered_entries),
                    "error_count": errors,
                    "status_counts": dict(status_counts),
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        stream.flush()

    print(f"Saved crawl data: {output.resolve()}")
    print(
        f"Summary: unique sitemap URLs={len(unique_entries)}, "
        f"checked={len(ordered_entries)}, errors={errors}"
    )


if __name__ == "__main__":
    main()
