'use strict';
// Per-tab observations stay local; only transport fault settings are shared.
window.pulseDiagnostics = (() => {
  const $ = id => document.getElementById(id);
  const base = new URL('./', window.location.href);
  const wsUrl = new URL('ws', base);
  wsUrl.protocol = wsUrl.protocol === 'https:' ? 'wss:' : 'ws:';
  const cleanUrl = url => `${url.origin}${url.pathname}`;
  const bounded = (items, item) => { items.push(item); if (items.length > 100) items.shift(); };
  const clock = () => new Date().toISOString();
  const observations = { api: [], rtt: [], connections: [] };
  const pending = new Map();
  let snapshot = null;
  let network = null;
  let networkCheckedAt = null;
  let telemetryAt = null;
  let connected = false;
  let opens = 0;
  let attempts = 0;
  let pingId = 0;
  let lastPong = null;
  let faultBusy = false;
  let probeBusy = false;
  $('diag-url-page').textContent = cleanUrl(new URL(window.location.href));
  $('diag-url-api').textContent = cleanUrl(new URL('api/', base));
  $('diag-url-ws').textContent = cleanUrl(wsUrl);

  function notice(message, failed = false) {
    $('diag-notice').hidden = !message;
    $('diag-notice').textContent = message;
    $('diag-notice').classList.toggle('failed', failed);
  }
  function record(kind, detail) { bounded(observations[kind], { time: clock(), ...detail }); }

  async function request(path, method = 'GET', body) {
    const start = performance.now();
    let status = null;
    let failure = null;
    try {
      const response = await fetch(new URL(path, base), {
        method,
        headers: body ? { 'Content-Type': 'application/json' } : {},
        body: body ? JSON.stringify(body) : undefined,
        signal: AbortSignal.timeout(10000),
        cache: 'no-store',
      });
      status = response.status;
      if (!response.ok) throw new Error(`Request failed (HTTP ${status}).`);
      return await response.json();
    } catch (e) {
      failure = e.name === 'TimeoutError' || e.name === 'AbortError'
        ? 'Request timed out after 10 seconds.' : e.message || 'Network request failed.';
      throw new Error(failure);
    } finally {
      // Diagnostic polling must not hide the latency of the requests being tested.
      if (!path.startsWith('api/diagnostics')) {
        const duration = Math.round((performance.now() - start) * 10) / 10;
        record('api', { path, method, status, duration_ms: duration, error: failure });
        $('diag-api-latency').textContent = `${duration.toFixed(1)} ms`;
        $('diag-api-result').textContent = failure || `${method} ${path} · HTTP ${status}`;
      }
    }
  }
  function updateNetwork(data) {
    network = data;
    networkCheckedAt = clock();
    $('diag-clients').textContent = data.connected_clients;
    const faults = data.faults;
    const active = [];
    if (faults.delay_ms) active.push(`API delay ${faults.delay_ms} ms`);
    if (faults.error_status) active.push(`HTTP ${faults.error_status}`);
    if (faults.pause_stream) active.push('Broadcasts paused');
    $('diag-fault-status').textContent = active.length
      ? `${active.join(' · ')} · auto-clear in ${Math.ceil(data.remaining_seconds)} s`
      : 'Transport normal · No active faults';
    $('diag-fault-status').classList.toggle('active', active.length > 0);
  }
  async function refreshNetwork() {
    try { updateNetwork(await request('api/diagnostics')); }
    catch {
      $('diag-fault-status').textContent = 'Transport settings unavailable · Retrying';
      $('diag-clients').textContent = '—';
    }
    setTimeout(refreshNetwork, 2000);
  }
  function lockFaultControls() {
    for (const id of ['diag-apply', 'diag-clear', 'diag-disconnect']) $(id).disabled = faultBusy;
  }
  async function control(callback) {
    if (faultBusy) return;
    faultBusy = true;
    lockFaultControls();
    notice('');
    try { await callback(); }
    catch (e) { notice(e.message, true); }
    finally { faultBusy = false; lockFaultControls(); }
  }
  async function probe() {
    if (probeBusy) return;
    probeBusy = true;
    $('diag-probe').disabled = true;
    try {
      await request('api/health');
      notice('API probe succeeded. Latency is shown above.');
    } catch (e) { notice(`API probe: ${e.message}`, true); }
    finally { probeBusy = false; $('diag-probe').disabled = false; }
  }
  $('diag-form').addEventListener('submit', event => {
    event.preventDefault();
    control(async () => {
      const data = await request('api/diagnostics/faults', 'PUT', {
        delay_ms: Number($('diag-delay').value),
        error_status: Number($('diag-error').value),
        pause_stream: $('diag-silence').checked,
        duration: Number($('diag-duration').value),
      });
      updateNetwork(data);
      notice('Transport settings applied. Use Test API to observe HTTP faults.');
    });
  });
  $('diag-clear').addEventListener('click', () => control(async () => {
    updateNetwork(await request('api/diagnostics/faults', 'DELETE'));
    $('diag-delay').value = '0';
    $('diag-error').value = '0';
    $('diag-silence').checked = false;
    notice('Transport faults cleared. Any request already in flight may still finish with its original fault.');
  }));
  $('diag-disconnect').addEventListener('click', () => control(async () => {
    const result = await request('api/diagnostics/disconnect', 'POST');
    notice(`Disconnect requested for ${result.disconnected_clients} socket(s). Clients will reconnect automatically.`);
  }));
  $('diag-probe').addEventListener('click', probe);

  function report() {
    return {
      version: 1,
      exported_at: clock(),
      environment: {
        page_url: cleanUrl(new URL(window.location.href)),
        api_url: cleanUrl(new URL('api/', base)),
        websocket_url: cleanUrl(wsUrl),
        browser_online: navigator.onLine,
        user_agent: navigator.userAgent,
      },
      connection: {
        connected, connection_attempts: attempts, successful_reconnections: Math.max(0, opens - 1),
        last_telemetry_received_at: telemetryAt?.wall ?? null,
        telemetry_age_ms: telemetryAt ? Math.round(performance.now() - telemetryAt.monotonic) : null,
      },
      network: { checked_at: networkCheckedAt, last_observed_status: network },
      observations,
      snapshot,
    };
  }
  $('diag-download').addEventListener('click', () => {
    const blob = new Blob([JSON.stringify(report(), null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `pulse-diagnostics-${new Date().toISOString().replace(/[:.]/g, '-')}.json`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    notice('Report downloaded with this tab’s observations and last received snapshot.');
  });

  setInterval(() => {
    if (telemetryAt) {
      const age = (performance.now() - telemetryAt.monotonic) / 1000;
      $('diag-age').textContent = `${age.toFixed(1)} s`;
      const stale = age > Math.max(5, (snapshot?.settings.interval || 1) * 2);
      $('diag-freshness').textContent = stale ? 'No recent telemetry · data may be stale' : 'Receiving telemetry';
      $('diag-freshness').classList.toggle('stale', stale);
    }
    if (!connected || lastPong === null || performance.now() - lastPong > 20000) {
      $('diag-ws-rtt').textContent = '—';
    }
  }, 1000);
  refreshNetwork();

  return {
    request,
    report,
    connecting() { attempts++; record('connections', { event: 'connecting' }); },
    opened() {
      connected = true;
      opens++;
      $('diag-reconnects').textContent = Math.max(0, opens - 1);
      record('connections', { event: 'open' });
    },
    closed(event) {
      connected = false;
      pending.clear();
      $('diag-ws-rtt').textContent = '—';
      record('connections', { event: 'close', code: event.code, reason: event.reason });
    },
    telemetry(data) {
      snapshot = data;
      telemetryAt = { monotonic: performance.now(), wall: clock() };
    },
    ping(socket) {
      const id = String(++pingId);
      if (pending.size >= 10) pending.delete(pending.keys().next().value);
      pending.set(id, performance.now());
      socket.send(JSON.stringify({ type: 'ping', id }));
    },
    pong(data) {
      const start = pending.get(data.id);
      if (start === undefined) return;
      pending.delete(data.id);
      const duration = Math.round((performance.now() - start) * 10) / 10;
      lastPong = performance.now();
      $('diag-ws-rtt').textContent = `${duration.toFixed(1)} ms`;
      record('rtt', { duration_ms: duration });
    },
  };
})();
