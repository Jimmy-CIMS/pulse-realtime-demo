'use strict';
const $ = (id) => document.getElementById(id);
// Resolve against the visible directory, preserving /proxy/8000/ and HTTPS.
const base = new URL('./', window.location.href);
let state = null;
let history = [];
let messages = 0;
let retry = 500;
let socket;
let heartbeat;
let watchdog;
let lastMessage = 0;
let busy = false;
const statuses = { healthy: 'Healthy', warning: 'Warning', offline: 'Offline' };
const timeLabel = (time) => new Date(time).toLocaleTimeString('en-GB', { hour12: false });

function error(message = '') {
  $('error').textContent = message;
  $('error').hidden = !message;
}
async function api(path, method = 'GET', body) {
  const response = await fetch(new URL(path, base), {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(10000),
  });
  if (!response.ok) throw new Error(`Request failed (HTTP ${response.status}). Please try again.`);
  return response.json();
}
function controls() {
  for (const id of ['pause', 'interval', 'target', 'spike', 'offline', 'recover']) {
    $(id).disabled = busy || !state;
  }
}
async function action(callback) {
  if (busy || !state) return;
  busy = true;
  controls();
  error();
  try { await callback(); }
  catch (e) { error(e.message || 'Request failed. Check the backend connection.'); }
  finally { busy = false; controls(); }
}
function metric(id, value, unit) {
  $(id).replaceChildren(document.createTextNode(String(value)), Object.assign(document.createElement('small'), { textContent: unit }));
}
function renderServers() {
  if (!state) return;
  const rows = state.servers.filter(s => $('filter').value === 'all' || s.status === $('filter').value);
  // All interpolated strings originate from fixed server metadata and validated numeric metrics.
  $('servers').innerHTML = rows.map(s => `<tr><td>${s.name}<span class="region">${s.region} · ${s.id}</span></td><td><span class="badge ${s.status}">${statuses[s.status]}</span></td><td><div class="usage"><span class="bar ${s.cpu >= 80 ? 'warn' : ''}"><span style="width:${s.cpu}%"></span></span>${s.cpu.toFixed(1)}%</div></td><td>${s.memory.toFixed(1)}%</td><td>${s.network.toFixed(1)}</td></tr>`).join('') || '<tr><td colspan="5">No servers match this status.</td></tr>';
}
function apply(data) {
  state = data;
  if (data.history) history = data.history;
  if (data.point && (!history.length || history[history.length - 1].time !== data.point.time)) history.push(data.point);
  history = history.filter(p => Date.parse(p.time) >= Date.now() - 300000).slice(-1201);
  const s = data.summary;
  metric('online', s.online, ' / 6');
  metric('cpu', s.cpu.toFixed(1), '%');
  metric('memory', s.memory.toFixed(1), '%');
  metric('network', s.network.toFixed(1), ' Mbps');
  $('warning-count').textContent = s.warnings ? `${s.warnings} ${s.warnings === 1 ? 'server needs' : 'servers need'} attention` : 'All servers are healthy';
  $('clock').textContent = `Last synced ${timeLabel(data.time)}`;
  $('stream-status').textContent = data.settings.paused ? 'Simulation paused' : `Updating every ${data.settings.interval} s`;
  $('pause').innerHTML = data.settings.paused ? 'Resume simulation <span>▶</span>' : 'Pause simulation <span>Ⅱ</span>';
  $('interval').value = data.settings.interval;
  if ($('target').options[0]?.value !== data.servers[0].id) {
    $('target').replaceChildren(...data.servers.map(s => new Option(s.name, s.id)));
  }
  renderServers();
  $('events').replaceChildren(...data.events.map(event => {
    const item = document.createElement('div');
    item.className = `event ${event.level}`;
    const time = document.createElement('time');
    time.dateTime = event.time;
    time.textContent = timeLabel(event.time);
    const p = document.createElement('p');
    p.textContent = event.message;
    item.append(time, p);
    return item;
  }));
  controls();
  draw();
}
function draw() {
  const canvas = $('chart');
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const ctx = canvas.getContext('2d');
  ctx.scale(ratio, ratio);
  const left = 30, right = width - 8, top = 12, bottom = height - 25;
  const end = Date.now(), start = end - 300000;
  ctx.font = '10px "Segoe UI", sans-serif';
  ctx.fillStyle = '#71806d';
  for (let value = 0; value <= 100; value += 25) {
    const y = bottom - value / 100 * (bottom - top);
    ctx.beginPath(); ctx.strokeStyle = '#dce1d6'; ctx.setLineDash([3, 4]);
    ctx.moveTo(left, y); ctx.lineTo(right, y); ctx.stroke();
    ctx.fillText(String(value), 0, y + 3);
  }
  ctx.setLineDash([]);
  for (let i = 0; i <= 4; i++) {
    const t = start + i / 4 * 300000;
    const label = new Date(t).toLocaleTimeString('en-GB', { hour12: false, hour: '2-digit', minute: '2-digit' });
    ctx.textAlign = i === 0 ? 'left' : i === 4 ? 'right' : 'center';
    ctx.fillText(label, left + i / 4 * (right - left), height - 4);
  }
  $('chart-empty').hidden = history.length > 1;
  if (history.length === 1) $('chart-empty').textContent = 'First sample received. Trends will build over time.';
  for (const [key, color] of [['memory', '#bbaa63'], ['cpu', '#456d45']]) {
    ctx.beginPath(); ctx.strokeStyle = color; ctx.lineWidth = 2;
    history.forEach((p, i) => {
      const x = left + (Date.parse(p.time) - start) / 300000 * (right - left);
      const y = bottom - p[key] / 100 * (bottom - top);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
    const last = history.at(-1);
    if (last) {
      ctx.beginPath(); ctx.fillStyle = color;
      ctx.arc(left + (Date.parse(last.time) - start) / 300000 * (right - left), bottom - last[key] / 100 * (bottom - top), 3, 0, Math.PI * 2); ctx.fill();
    }
  }
}
function connect() {
  const url = new URL('ws', base);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  $('connection').textContent = 'Connecting';
  $('connection').classList.remove('live');
  socket = new WebSocket(url);
  socket.onopen = () => {
    lastMessage = Date.now();
    heartbeat = setInterval(() => {
      if (socket.readyState === WebSocket.OPEN) socket.send('ping');
    }, 15000);
    watchdog = setInterval(() => {
      if (Date.now() - lastMessage > 20000) socket.close();
    }, 5000);
  };
  socket.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      apply(data);
      lastMessage = Date.now();
      retry = 500;
      messages++;
      $('messages').textContent = messages.toLocaleString('en-GB');
      $('connection').textContent = 'Stream connected';
      $('connection').classList.add('live');
    } catch { error('Could not read the incoming data. Please refresh.'); }
  };
  socket.onerror = () => socket.close();
  socket.onclose = () => {
    clearInterval(heartbeat);
    clearInterval(watchdog);
    $('connection').textContent = 'Disconnected · Retrying';
    $('connection').classList.remove('live');
    $('stream-status').textContent = 'Data may be stale';
    setTimeout(connect, retry + Math.random() * 300);
    retry = Math.min(retry * 2, 10000);
  };
}
$('filter').addEventListener('change', renderServers);
$('pause').addEventListener('click', () => action(async () => {
  await api('api/settings', 'PUT', { ...state.settings, paused: !state.settings.paused });
  apply(await api('api/snapshot'));
}));
$('interval').addEventListener('change', () => {
  const interval = Number($('interval').value);
  action(async () => {
    try { await api('api/settings', 'PUT', { ...state.settings, interval }); }
    finally { apply(await api('api/snapshot')); }
  });
});
for (const [id, kind] of [['spike', 'cpu'], ['offline', 'offline'], ['recover', 'recover']]) {
  $(id).addEventListener('click', () => action(async () => {
    apply(await api('api/incidents', 'POST', { server_id: $('target').value, kind }));
  }));
}
new ResizeObserver(draw).observe($('chart').parentElement);
// HTTP bootstrap also keeps the controls usable when a proxy blocks WebSockets.
api('api/snapshot').then(data => { if (!state) apply(data); }).catch(() => error('Could not load initial data. Check the backend service and proxy path.'));
connect();
