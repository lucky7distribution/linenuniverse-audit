const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.events = {}; this.value = ''; this.classList = { add() {}, remove() {} }; }
  appendChild(e) { this.children.push(e); return e; }
  replaceChildren(...items) { this.children = items; }
  insertRow() { return this.appendChild(new Element('tr')); }
  insertCell() { return this.appendChild(new Element('td')); }
  addEventListener(name, fn) { this.events[name] = fn; }
}

test('dashboard renders the measured run, comparisons, filters and reload without duplicating rows', async () => {
  const root = path.resolve(__dirname, '..');
  const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
  const ids = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(m => [m[1], new Element('div')]));
  const summary = JSON.parse(fs.readFileSync(path.join(root, 'audit-data/dashboard-summary.json'), 'utf8'));
  const inventory = JSON.parse(fs.readFileSync(path.join(root, 'audit-data/url-inventory.json'), 'utf8'));
  const manifest = JSON.parse(fs.readFileSync(path.join(root, 'audit-data/latest.json'), 'utf8'));
  const context = vm.createContext({
    document: { getElementById: id => ids.get(id), createElement: tag => new Element(tag), querySelectorAll: () => [] },
    Option: function(text, value) { this.textContent = text; this.value = value; },
    LinenAuditData: { load: async () => ({ summary, inventory, manifest }) },
    URL, Date, Intl, Blob, window: { setTimeout },
  });
  const inline = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n');
  vm.runInContext(inline, context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ids.get('metric-description').textContent, summary.metrics.missing_meta_descriptions.toLocaleString('en-US'));
  assert.equal(ids.get('metric-broken').textContent, summary.metrics.broken_category_destinations.toLocaleString('en-US'));
  assert.ok(ids.get('freshness-note').textContent.includes(summary.audit.run_id));
  assert.equal(ids.get('issue-rows').children.length, summary.issues.length);
  assert.equal(ids.get('download-summary').href, manifest.summary_url);
  assert.equal(ids.get('url-rows').children.length, Math.min(50, inventory.length));
  assert.equal(ids.get('comparison-rows').children.length, 6);
  ids.get('url-status').value = '404'; ids.get('url-status').events.change();
  assert.equal(ids.get('url-rows').children.length, inventory.filter(row => row.status === 404).length);
  const count = ids.get('seo-coverage').children.length;
  await ids.get('reload-results').events.click();
  assert.equal(ids.get('seo-coverage').children.length, count);
  assert.equal(ids.get('reload-results').disabled, false);
});
