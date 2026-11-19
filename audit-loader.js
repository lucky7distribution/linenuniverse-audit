(function (root) {
  "use strict";
  function dataPath(value) {
    if (typeof value !== "string" || !/^\/audit-data\/runs\/[a-zA-Z0-9_-]+\/(dashboard-summary|url-inventory)\.json$/.test(value)) {
      throw new Error("Invalid audit dataset path.");
    }
    return value;
  }

  async function freshJson(path, expectedHash, fetcher, cryptoProvider) {
    const separator = path.includes("?") ? "&" : "?";
    const nonce = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const response = await fetcher(`${path}${separator}fresh=${nonce}`, {
      cache: "no-store", headers: { "Cache-Control": "no-cache" },
    });
    if (!response.ok) throw new Error(`Audit data request failed (${response.status}).`);
    const bytes = await response.arrayBuffer();
    if (expectedHash) {
      const digest = await cryptoProvider.subtle.digest("SHA-256", bytes);
      const actual = Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
      if (actual !== expectedHash) throw new Error("Audit files do not match this run. Reload after deployment completes.");
    }
    return JSON.parse(new TextDecoder().decode(bytes));
  }

  async function load(selectedManifest, fetcher = root.fetch.bind(root), cryptoProvider = root.crypto) {
    const manifest = selectedManifest || await freshJson("/audit-data/latest.json", null, fetcher, cryptoProvider);
    const [summary, inventory] = await Promise.all([
      freshJson(dataPath(manifest.summary_url), manifest.summary_sha256, fetcher, cryptoProvider),
      freshJson(dataPath(manifest.inventory_url), manifest.inventory_sha256, fetcher, cryptoProvider),
    ]);
    if (manifest.run_id !== summary.audit.run_id || !Array.isArray(inventory)) throw new Error("Audit run identity mismatch.");
    return { manifest, summary, inventory };
  }
  const api = { load, freshJson, dataPath };
  root.LinenAuditData = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(globalThis);
