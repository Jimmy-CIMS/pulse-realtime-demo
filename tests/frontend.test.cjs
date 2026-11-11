// Behavioral checks for the actual browser scripts, using a small DOM/transport harness.
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

function snapshot() {
  const point = { time: new Date().toISOString(), cpu: 30, memory: 50 };
  return { type: 'snapshot', time: point.time, sequence: 1, settings: { paused: false, interval: 1 },
    summary: { online: 6, cpu: 30, memory: 50, network: 200, warnings: 0 },
    history: [point], point, events: [], servers: Array.from({ length: 6 }, (_, i) => ({
      id: `srv-0${i + 1}`, name: `api-0${i + 1}`, region: 'Taipei', status: 'healthy',
      cpu: 30, memory: 50, network: 20,
    })) };
}
const normal = () => ({ faults: { delay_ms: 0, error_status: 0, pause_stream: false, duration: 30 },
  remaining_seconds: 0, connected_clients: 1 });
const flush = () => new Promise(resolve => setImmediate(resolve));
function harness(page = 'https://dev.example/proxy/8000/?token=secret#private') {
  const nodes = new Map(), requests = [], sockets = [], timers = [], intervals = [], downloads = [];
  const behavior = { healthStatus: 200, clock: 0 };
  function element() {
    const classes = new Set();
    return { hidden: false, disabled: false, checked: false, value: 'all', options: [],
      parentElement: {}, clientWidth: 700, clientHeight: 225, listeners: {},
      classList: { add(name) { classes.add(name); }, remove(name) { classes.delete(name); },
        toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); },
        contains(name) { return classes.has(name); } },
      addEventListener(name, fn) { this.listeners[name] = fn; },
      replaceChildren(...children) { this.nodes = children; this.options = children; },
      append() {}, click() { downloads.push(this); }, remove() {},
      getContext() { return new Proxy({}, { get: () => () => {} }); },
    };
  }
  const $ = id => { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); };
  let exported;
  class BrowserURL extends URL {
    static createObjectURL(blob) { exported = blob; return 'blob:report'; }
    static revokeObjectURL() {}
  }
  const context = { URL: BrowserURL, AbortSignal, Blob, Date, Math, JSON, Number, String, Promise, Map,
    performance: { now: () => behavior.clock }, navigator: { onLine: true, userAgent: 'Test browser' },
    Option: function (name, id) { this.value = id; this.textContent = name; },
    window: { location: { href: page }, devicePixelRatio: 1 },
    document: { getElementById: $, createTextNode: text => ({ textContent: text }),
      createElement: element, body: { append() {} } },
    ResizeObserver: class { observe() {} },
    fetch: async (url, options) => {
      requests.push({ url: String(url), ...options });
      behavior.clock += 25;
      const pathname = new URL(url).pathname;
      const health = pathname.endsWith('/health');
      const status = health ? behavior.healthStatus : 200;
      return { ok: status < 400, status, json: async () => {
        if (pathname.endsWith('/snapshot')) return snapshot();
        if (pathname.endsWith('/settings') && options.method === 'PUT') return JSON.parse(options.body);
        if (pathname.endsWith('/disconnect')) return { disconnected_clients: 1 };
        if (pathname.endsWith('/faults') && options.method === 'PUT') {
          return { ...normal(), faults: JSON.parse(options.body), remaining_seconds: 30 };
        }
        return normal();
      } };
    },
    WebSocket: class {
      static OPEN = 1;
      constructor(url) { this.url = String(url); this.readyState = 1; this.sent = []; sockets.push(this); }
      send(text) { this.sent.push(JSON.parse(text)); }
      close() {}
    },
    setInterval(fn, delay) { intervals.push({ fn, delay }); return intervals.length; },
    clearInterval() {},
    setTimeout(fn, delay) { timers.push({ fn, delay }); return timers.length; },
  };
  vm.createContext(context);
  for (const name of ['diagnostics.js', 'app.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../app/static', name), 'utf8'), context);
  }
  return { $, context, requests, sockets, timers, intervals, behavior, downloads,
    exported: () => exported, diagnostics: context.window.pulseDiagnostics };
}

test('proxy URLs and exported reports omit query strings and fragments', async () => {
  for (const page of ['http://localhost:8000/', 'https://dev.example/proxy/8000/?token=secret#private',
    'https://dev.example/code/proxy/8000/']) {
    const h = harness(page);
    await flush();
    const expected = new URL('ws', new URL('./', page));
    expected.protocol = expected.protocol === 'https:' ? 'wss:' : 'ws:';
    assert.equal(h.sockets[0].url, String(expected));
    assert.ok(h.requests.some(request => request.url.endsWith('/api/snapshot')));
    const report = JSON.stringify(h.diagnostics.report());
    assert.ok(!report.includes('secret') && !report.includes('#private'));
  }
});

test('pong measurements preserve telemetry and count successful reconnections', async () => {
  const h = harness();
  await flush();
  const socket = h.sockets[0];
  socket.onopen();
  socket.onmessage({ data: JSON.stringify(snapshot()) });
  assert.equal(h.$('messages').textContent, '1');
  const ping = socket.sent[0];
  h.behavior.clock += 12;
  socket.onmessage({ data: JSON.stringify({ type: 'pong', id: ping.id }) });
  assert.equal(h.$('diag-ws-rtt').textContent, '12.0 ms');
  assert.equal(h.$('messages').textContent, '1');
  assert.equal(h.diagnostics.report().snapshot.sequence, 1);
  socket.onclose({ code: 1012, reason: 'Simulated transport disconnect' });
  assert.equal(h.$('diag-ws-rtt').textContent, '—');
  h.timers.find(timer => timer.delay >= 500 && timer.delay < 1000).fn();
  h.sockets[1].onopen();
  assert.equal(h.$('diag-reconnects').textContent, 1);
  assert.equal(h.diagnostics.report().connection.successful_reconnections, 1);
});

test('fault controls submit settings and failed API probes show measured errors', async () => {
  const h = harness();
  await flush();
  h.$('diag-delay').value = '2000';
  h.$('diag-error').value = '503';
  h.$('diag-duration').value = '30';
  h.$('diag-silence').checked = true;
  h.$('diag-form').listeners.submit({ preventDefault() {} });
  await flush();
  const call = h.requests.find(request => request.method === 'PUT');
  assert.deepEqual(JSON.parse(call.body), { delay_ms: 2000, error_status: 503, duration: 30, pause_stream: true });
  assert.match(h.$('diag-fault-status').textContent, /Broadcasts paused/);
  h.behavior.healthStatus = 503;
  await h.$('diag-probe').listeners.click();
  assert.match(h.$('diag-api-result').textContent, /HTTP 503/);
  assert.match(h.$('diag-notice').textContent, /API probe/);
  assert.equal(h.$('diag-probe').disabled, false);
  assert.equal(h.diagnostics.report().observations.api.at(-1).status, 503);
  const snapshotReads = h.requests.filter(request => request.url.endsWith('/snapshot')).length;
  await h.$('pause').listeners.click();
  assert.match(h.$('pause').innerHTML, /Resume simulation/);
  assert.equal(h.requests.filter(request => request.url.endsWith('/snapshot')).length, snapshotReads);
  await h.$('diag-clear').listeners.click();
  await flush();
  assert.match(h.$('diag-fault-status').textContent, /No active faults/);
});

test('stale telemetry is visible and JSON download works without a backend fetch', async () => {
  const h = harness();
  await flush();
  h.behavior.clock += 7000;
  h.intervals.find(timer => timer.delay === 1000).fn();
  assert.match(h.$('diag-freshness').textContent, /stale/);
  const calls = h.requests.length;
  h.$('diag-download').listeners.click();
  assert.equal(h.requests.length, calls);
  assert.equal(h.downloads.length, 1);
  assert.match(h.downloads[0].download, /^pulse-diagnostics-.*\.json$/);
  const report = JSON.parse(await h.exported().text());
  assert.equal(report.version, 1);
  assert.equal(report.snapshot.servers.length, 6);
  assert.ok(report.connection.telemetry_age_ms >= 7000);
});

test('API observations stay bounded and exclude diagnostic polling', async () => {
  const h = harness();
  await flush();
  for (let i = 0; i < 105; i++) await h.diagnostics.request('api/health');
  assert.equal(h.diagnostics.report().observations.api.length, 100);
  await h.diagnostics.request('api/diagnostics');
  assert.equal(h.diagnostics.report().observations.api.length, 100);
  assert.equal(h.diagnostics.report().observations.api.at(-1).path, 'api/health');
});
