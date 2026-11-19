const { test } = require('node:test');
const assert = require('node:assert/strict');
const { webcrypto, createHash } = require('node:crypto');
const loader = require('../audit-loader.js');

function fixture() {
  const summary = JSON.stringify({ audit: { run_id: 'new-run' }, metrics: {} });
  const inventory = '[]';
  const hash = (s) => createHash('sha256').update(s).digest('hex');
  return { run_id: 'new-run', summary_url: '/audit-data/runs/new-run/dashboard-summary.json', inventory_url: '/audit-data/runs/new-run/url-inventory.json', summary_sha256: hash(summary), inventory_sha256: hash(inventory), summary, inventory };
}

test('loads one consistent run with cache bypass on every request', async () => {
  const f = fixture(); const calls = [];
  const fetcher = async (url, options) => {
    calls.push([url, options]);
    const text = url.startsWith('/audit-data/latest.json') ? JSON.stringify(f) : url.includes('dashboard-summary') ? f.summary : f.inventory;
    return new Response(text);
  };
  const result = await loader.load(null, fetcher, webcrypto);
  assert.equal(result.summary.audit.run_id, 'new-run');
  assert.equal(calls.length, 3);
  for (const [url, options] of calls) { assert.ok(url.includes('?fresh=')); assert.equal(options.cache, 'no-store'); }
});

test('refuses stale dataset bytes instead of silently showing them', async () => {
  const f = fixture();
  await assert.rejects(loader.load(f, async () => new Response('{}'), webcrypto), /do not match/);
});

test('refuses a failed latest pointer without using old static defaults', async () => {
  let requests = 0;
  await assert.rejects(loader.load(null, async () => { requests++; return new Response('', { status: 404 }); }, webcrypto), /404/);
  assert.equal(requests, 1);
});

test('rejects external or traversal dataset paths', () => {
  for (const value of ['https://evil.test/data.json', '/audit-data/runs/../../secret/dashboard-summary.json', '//evil.test/a']) assert.throws(() => loader.dataPath(value));
});

test('refuses mismatched run identity even with valid checksums', async () => {
  const f = fixture(); f.run_id = 'wrong-run';
  await assert.rejects(loader.load(f, async (url) => new Response(url.includes('dashboard-summary') ? f.summary : f.inventory), webcrypto), /identity mismatch/);
});
