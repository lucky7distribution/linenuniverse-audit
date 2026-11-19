#!/usr/bin/env python3
"""Create new evidence files, then promote only a complete audit to the dashboard."""
import argparse
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delay", type=float, default=0.5)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    sitemap = root / "audit-data" / f"sitemap-crawl-{tag}.jsonl"
    categories = root / "audit-data" / f"linked-category-crawl-{tag}.jsonl"
    commands = [
        ["crawl_sitemap.py", "--delay", str(args.delay), "--workers", "3", "--output", str(sitemap)],
        ["crawl_linked_categories.py", str(sitemap), "--delay", str(args.delay), "--output", str(categories)],
        ["build_dashboard_data.py", str(sitemap), str(categories)],
        ["validate_audit.py"],
    ]
    for script, *arguments in commands:
        subprocess.run([sys.executable, str(root / "scripts" / script), *arguments], cwd=root, check=True)
    print("Fresh audit prepared. Commit and push this repository to publish through its existing Vercel integration.")


if __name__ == "__main__":
    main()
