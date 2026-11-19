"""Validate the exact versioned datasets selected by latest.json, without network access."""
import hashlib
import json
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent.parent
    manifest = json.loads((root / "audit-data/latest.json").read_text(encoding="utf-8"))
    values = {}
    for key in ("summary", "inventory"):
        path = (root / manifest[key + "_url"].lstrip("/")).resolve()
        if not path.is_relative_to(root / "audit-data/runs"):
            raise ValueError("Manifest points outside audit history")
        raw = path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == manifest[key + "_sha256"], "Dataset checksum mismatch"
        values[key] = json.loads(raw)
    data, inventory = values["summary"], values["inventory"]
    assert data["audit"]["run_id"] == manifest["run_id"]
    assert len(inventory) == len({row["url"] for row in inventory}), "Inventory contains duplicate URL rows"
    assert len(inventory) == data["audit"]["sitemap_urls_checked"] + data["audit"]["linked_category_urls_checked"]
    assert data["metrics"]["missing_meta_descriptions"] == sum(not (row["meta_description"] or "").strip() for row in inventory if row["source"] == "sitemap" and row["status"] == 200 and row["url"] == row["final_url"] and row["page_type"] != "Feed")
    for item in data["issues"]:
        assert item["affected_url_count"] == len(set(item["affected_urls"])) > 0
        assert item["last_verified_at"] == data["audit"]["completed_at"]
        assert all(url in {row["url"] for row in inventory} for url in item["affected_urls"])
    assert (root / "audit-data/dashboard-summary.json").read_bytes() == (root / manifest["summary_url"].lstrip("/")).read_bytes()
    assert (root / "audit-data/url-inventory.json").read_bytes() == (root / manifest["inventory_url"].lstrip("/")).read_bytes()
    print(f"Validated run {manifest['run_id']}: {len(inventory)} URLs, {len(data['issues'])} issue groups, matching SHA-256 datasets.")


if __name__ == "__main__":
    main()
