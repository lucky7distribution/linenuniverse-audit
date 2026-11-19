# Linen Universe public website audit

Existing dashboard: https://linenuniverse-audit.vercel.app/

## Refresh the report

With Python 3.12+ installed, run from this repository:

```powershell
python scripts/run_audit.py
```

This retrieves robots.txt and the sitemap again, crawls all current sitemap URLs,
discovers category links in their fresh HTML, and checks the category URLs absent
from the sitemap. It does not submit forms, log in, place orders or change Odoo.
Request starts share a minimum 0.5-second interval. HTTP 404, 429 and server errors
are checked again with a fresh request. Timeout/network failures remain unverified.

After validation, commit and push this repository to its existing Vercel-linked
branch. Odoo.sh and the separate Odoo modules repository do not publish this app.
Alternatively, after this workflow is pushed, an authorized repository maintainer
can use **GitHub → Actions → Refresh public website audit → Run workflow**.
The workflow uses the repository's built-in token, with no production Odoo credentials.
If branch protection or GitHub-triggered Vercel builds block its push/deployment,
download the action artifact and publish through the repository's permitted process.

## Freshness and cache handling

- Every crawl uses new HTTP requests, `Cache-Control: no-cache, no-store, max-age=0`,
  `Pragma: no-cache`, and a unique `_linen_audit` query nonce per request.
- The nonce is removed from reported URLs. Real product/filter parameters are preserved.
- Each record stores its UTC check time, response cache headers and response SHA-256.
  Origin-side caches, including Odoo's generated sitemap cache, cannot be forcibly
  purged by this public crawler. No claim of an origin cache purge is made.
- Raw JSONL evidence files are retained. There is no response cache or old-results fallback.
- Incomplete/mismatched runs are rejected before replacing the published dataset.
- Completed runs are saved under `audit-data/runs/<run-id>/`. `latest.json` selects
  both datasets together and contains their hashes; the browser verifies them.
- Vercel data routes use browser/CDN `no-store`. The loader also uses unique query
  nonces and `fetch(..., {cache: 'no-store'})`.
- **Reload published results** fetches the latest published dataset. It does not
  start a crawl or turn an old report into new measurements. The report displays
  the crawl timestamp and warns when it is more than 24 hours old.
- `audit-data/history.json` lists retained reports. Comparisons distinguish
  no-longer-detected URL findings from proven repairs to old destinations.

## Scope and interpretation

This refresh follows the existing application's scope: sitemap pages and linked
non-sitemap category paths, excluding category query/pagination combinations.
It is not a complete arbitrary-link crawl. Off-host canonical tags, metadata,
H1 headings, linked-category HTTP errors/redirects and image alternative markup
are measured from fresh public HTML. Duplicate metadata needs intent review.
Missing alt attributes are separate from explicit empty alternatives; decorative
markers are excluded, and empty alternatives require contextual review.
The older combined missing/empty-alt count is not directly comparable.

External-link status, full schema validation, rendered/mobile functionality,
Core Web Vitals and Google indexing/traffic remain unmeasured. No overall score
is invented. This update does not implement every feature in the master brief.

## Verify locally

```powershell
python -m unittest discover -s tests
node --test tests/audit-loader.test.cjs
node --test tests/dashboard.test.cjs
python scripts/validate_audit.py
python -m http.server 8080
```

Open http://localhost:8080/. After deployment, check `/audit-data/latest.json` and
confirm its run ID matches the one displayed by the dashboard. Its UTC completion
date must be the new crawl date; simply seeing a new deployment date is insufficient.
