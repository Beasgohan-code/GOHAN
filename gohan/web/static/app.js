/* ==========================================================================
   GOHAN control panel
   Vanilla JS, no framework, no build step. Everything is rendered from the
   JSON API in gohan/web/api.py.
   ========================================================================== */
'use strict';

/* ------------------------------------------------------------- helpers --- */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const esc = (value) =>
  String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const num = (value) => {
  const n = Number(value ?? 0);
  if (!Number.isFinite(n)) return String(value ?? '—');
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
  if (Math.abs(n) >= 1e4) return (n / 1e3).toFixed(1).replace(/\.0$/, '') + 'k';
  return n.toLocaleString('en-US');
};

const duration = (seconds) => {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600),
        m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${sec}s`;
  return `${sec}s`;
};

const clock = (iso) => {
  try {
    return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch { return ''; }
};

const debounce = (fn, ms = 220) => {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
};

const TAG_ICONS = {
  ScamDeleted: '🚫', CaptchaPassed: '🔢', CaptchaFailed: '⚠️', Warning: '⚠️', Warn: '⚠️',
  RaidAlert: '🚨', Lockdown: '🔒', FilterHit: '🧲', GameScore: '🎮', AiAnswer: '🤖',
  GroupAdded: '➕', GroupRemoved: '➖', GroupPromoted: '👑', WatchdogRun: '🧹', LowDisk: '💾',
  MusicDownload: '🎧', MusicSearch: '🔍', NoteSaved: '📝', AnimeSearch: '🎬', BotStarted: '🚀',
  BotStopped: '🛑', NewUser: '👋', Broadcast: '📣', WebToggle: '🖱', Error: '❌',
  Maintenance: '🛠', OwnerRestart: '♻️', Suspect: '👁', Mute: '🔇', Ban: '⛔',
};
const tagIcon = (tag) => TAG_ICONS[tag] || '•';

/* ----------------------------------------------------------------- api --- */
async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  let data = {};
  try { data = await response.json(); } catch { /* empty body */ }
  if (!response.ok || data.ok === false) {
    const error = new Error(data.error || `request failed (${response.status})`);
    error.status = response.status;
    error.authMissing = Boolean(data.auth_required);
    throw error;
  }
  return data;
}

/* --------------------------------------------------------------- state --- */
const App = {
  page: 'overview',
  data: { overview: null, groups: null, events: null, settings: null, group: null },
  ui: {
    search: '', tag: '', paused: false, selected: 0, groupQuery: '',
    caseQuery: '', userQuery: '', userFilter: 'all', modDays: 7,
  },
  sse: null,
  poll: null,
  retry: 0,
};

/* -------------------------------------------------------------- toasts --- */
function toast(kind, title, message = '', ttl = 4200) {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.innerHTML = `<strong>${esc(title)}</strong>${message ? `<p>${esc(message)}</p>` : ''}`;
  $('#toasts').appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 250); }, ttl);
}

/* --------------------------------------------------------------- modal --- */
function showModal({ title, body, confirmLabel = 'Confirm', danger = false, onConfirm, input = null }) {
  const modal = $('#modal');
  $('#modal-title').textContent = title;
  $('#modal-body').innerHTML =
    (body ? `<p>${body}</p>` : '') +
    (input ? `<label class="field"><span>${esc(input.label)}</span><textarea id="modal-input" placeholder="${esc(input.placeholder || '')}"></textarea></label>` : '');
  const foot = $('#modal-foot');
  foot.innerHTML = '';
  const cancel = document.createElement('button');
  cancel.className = 'btn btn-ghost';
  cancel.textContent = 'Cancel';
  cancel.onclick = () => closeModal();
  const confirm = document.createElement('button');
  confirm.className = `btn ${danger ? 'btn-danger' : 'btn-primary'}`;
  confirm.textContent = confirmLabel;
  confirm.onclick = async () => {
    const value = input ? ($('#modal-input')?.value || '') : null;
    closeModal();
    await onConfirm?.(value);
  };
  foot.append(cancel, confirm);
  modal.hidden = false;
  setTimeout(() => (input ? $('#modal-input') : confirm).focus(), 40);
}
const closeModal = () => { $('#modal').hidden = true; };

/* -------------------------------------------------------------- drawer --- */
function openDrawer(title, html) {
  $('#drawer-title').textContent = title;
  $('#drawer-body').innerHTML = html;
  $('#drawer').hidden = false;
}
const closeDrawer = () => { $('#drawer').hidden = true; };

/* ------------------------------------------------------------ charting --- */
function drawChart(canvas, points) {
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 600;
  const height = canvas.clientHeight || 208;
  canvas.width = width * dpr;
  canvas.height = height * dpr;
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);

  if (!points?.length) return;
  const styles = getComputedStyle(document.documentElement);
  const accent = styles.getPropertyValue('--accent').trim() || '#4c8dff';
  const accent2 = styles.getPropertyValue('--accent-2').trim() || '#8b5cf6';
  const line = styles.getPropertyValue('--line').trim() || '#1e2534';
  const mut = styles.getPropertyValue('--mut').trim() || '#7d879c';

  const pad = { top: 16, right: 8, bottom: 26, left: 34 };
  const w = width - pad.left - pad.right;
  const h = height - pad.top - pad.bottom;
  const max = Math.max(...points.map((p) => p.value), 1) * 1.15;

  // grid + y labels
  ctx.font = '10px ui-sans-serif, system-ui, sans-serif';
  ctx.fillStyle = mut;
  ctx.strokeStyle = line;
  ctx.lineWidth = 1;
  for (let i = 0; i <= 3; i += 1) {
    const y = pad.top + (h / 3) * i;
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(width - pad.right, y);
    ctx.stroke();
    ctx.fillText(num(Math.round(max - (max / 3) * i)), 4, y + 3);
  }

  const x = (i) => pad.left + (w / Math.max(1, points.length - 1)) * i;
  const y = (v) => pad.top + h - (v / max) * h;

  // area
  const gradient = ctx.createLinearGradient(0, pad.top, 0, pad.top + h);
  gradient.addColorStop(0, `${accent}66`);
  gradient.addColorStop(1, `${accent}05`);
  ctx.beginPath();
  ctx.moveTo(x(0), y(points[0].value));
  points.forEach((p, i) => ctx.lineTo(x(i), y(p.value)));
  ctx.lineTo(x(points.length - 1), pad.top + h);
  ctx.lineTo(x(0), pad.top + h);
  ctx.closePath();
  ctx.fillStyle = gradient;
  ctx.fill();

  // line
  const stroke = ctx.createLinearGradient(pad.left, 0, width - pad.right, 0);
  stroke.addColorStop(0, accent);
  stroke.addColorStop(1, accent2);
  ctx.beginPath();
  points.forEach((p, i) => (i ? ctx.lineTo(x(i), y(p.value)) : ctx.moveTo(x(i), y(p.value))));
  ctx.strokeStyle = stroke;
  ctx.lineWidth = 2.4;
  ctx.lineJoin = 'round';
  ctx.stroke();

  // x labels
  ctx.fillStyle = mut;
  points.forEach((p, i) => {
    if (points.length > 8 && i % 2) return;
    ctx.fillText(p.label, x(i) - 12, height - 8);
  });

  // hover
  canvas.onmousemove = (event) => {
    const rect = canvas.getBoundingClientRect();
    const px = event.clientX - rect.left;
    let index = Math.round(((px - pad.left) / w) * (points.length - 1));
    index = Math.max(0, Math.min(points.length - 1, index));
    let tip = $('.chart-tip', canvas.parentElement);
    if (!tip) {
      tip = document.createElement('div');
      tip.className = 'chart-tip';
      canvas.parentElement.appendChild(tip);
    }
    tip.textContent = `${points[index].label} · ${num(points[index].value)} events`;
    tip.style.left = `${x(index)}px`;
    tip.style.top = `${y(points[index].value)}px`;
  };
  canvas.onmouseleave = () => $('.chart-tip', canvas.parentElement)?.remove();
}

/* ------------------------------------------------------------ fragments --- */
const switchHtml = (key, enabled, { scope = 'chat', chat = null } = {}) => `
  <label class="switch" title="${enabled ? 'on' : 'off'}">
    <input type="checkbox" ${enabled ? 'checked' : ''}
      data-toggle="module" data-key="${esc(key)}" data-scope="${esc(scope)}"${chat ? ` data-chat="${esc(chat)}"` : ''}>
    <span></span>
  </label>`;

const moduleCard = (mod) => `
  <article class="module ${mod.enabled ? 'on' : ''}">
    <div class="module-ico">${esc(mod.icon)}</div>
    <div class="module-body">
      <strong>${esc(mod.name)}</strong>
      <p>${esc(mod.blurb)}</p>
      <div class="module-meta">
        <span class="chip">${mod.scope === 'global' ? 'global' : 'per group'}</span>
        ${mod.command ? `<span class="chip mono">${esc(mod.command)}</span>` : ''}
        ${mod.stat ? `<span class="chip">${esc(mod.stat)}</span>` : ''}
      </div>
    </div>
    ${switchHtml(mod.key, mod.enabled, { scope: mod.scope })}
    ${mod.scope === 'chat' ? `
      <button class="icon-btn" style="width:30px;height:30px;font-size:13px"
        title="apply to every group"
        data-action="apply-all" data-key="${esc(mod.key)}" data-on="${mod.enabled ? '1' : '0'}">⇄</button>` : ''}
  </article>`;

const eventRow = (event) => `
  <div class="event">
    <div class="event-ico">${tagIcon(event.tag)}</div>
    <div class="event-main">
      <strong>${esc(event.tag)}</strong>
      <p>${esc(event.data || '')}${event.chat_id ? ` · <span class="mono">${esc(event.chat_id)}</span>` : ''}</p>
    </div>
    <div class="event-time">${esc(event.age || clock(event.ts))}</div>
  </div>`;

const kpiCard = (kpi) => `
  <article class="kpi">
    <div class="kpi-top"><span>${esc(kpi.icon)}</span>${esc(kpi.label)}</div>
    <div class="kpi-val">${num(kpi.value)}</div>
    <div class="kpi-delta">${esc(kpi.delta || '')}</div>
  </article>`;

const empty = (icon, text, hint = '') => `
  <div class="empty"><span class="big">${icon}</span>${esc(text)}${hint ? `<p class="muted">${esc(hint)}</p>` : ''}</div>`;

/* --------------------------------------------------------------- pages --- */
function pageOverview() {
  const d = App.data.overview;
  if (!d) return skeleton();
  const bot = d.bot || {};
  const onCount = d.modules.filter((m) => m.enabled).length;

  const demoNotice = d.demo ? `
    <section class="card" style="border-color:color-mix(in srgb, var(--warn) 35%, var(--line));background:color-mix(in srgb, var(--warn) 6%, var(--panel))">
      <div class="card-head" style="margin:0">
        <h2>🧪 demo preview</h2>
        <span class="sub">this page is showing sample data</span>
      </div>
      <p class="muted" style="margin:8px 0 0;font-size:13px">
        Start the bot with a <code>BOT_TOKEN</code> and the same panel fills with live numbers —
        the API, the toggles and the event stream all read from the running bot.
      </p>
    </section>` : '';

  return `
    ${demoNotice}
    <section class="kpis">${d.kpis.map(kpiCard).join('')}</section>

    <section class="two-col">
      <div class="card">
        <div class="card-head">
          <h2>Activity</h2>
          <span class="sub">last 24 h · events per 2 h</span>
        </div>
        <div class="chart-wrap"><canvas class="chart" id="chart"></canvas></div>
      </div>

      <div class="card">
        <div class="card-head">
          <h2>Runtime</h2>
          <span class="sub">${esc(bot.mode === 'demo' ? 'demo data' : 'live')}</span>
        </div>
        <div class="kv">
          ${kvRow('🧚 bot', esc(bot.name || 'GOHAN') + (bot.username ? ` <span class="mono">@${esc(bot.username)}</span>` : ''))}
          ${kvRow('⏱ uptime', esc(duration(bot.uptime_s)))}
          ${kvRow('🌍 host', esc(bot.host || '—'))}
          ${kvRow('📡 mtproto', esc(bot.mtproto || 'off'))}
          ${kvRow('🤖 assistant', esc(bot.assistant || 'demo generator'))}
          ${kvRow('🧹 watchdog', `${bot.sweeps || 0} sweeps`)}
          ${kvRow('🌐 self-ping', `${num(bot.pings || 0)} ok${bot.ping_failures ? ` · ${num(bot.ping_failures)} failed` : ''}`)}
          ${kvRow('👑 owners', String(bot.owners ?? 0))}
          ${kvRow('🧩 modules on', `${onCount} / ${d.modules.length}`)}
        </div>
      </div>
    </section>

    <section class="card">
      <div class="card-head">
        <h2>Modules</h2>
        <span class="sub">${onCount} of ${d.modules.length} enabled</span>
        <div class="card-actions"><a class="btn btn-ghost" href="#/modules">open modules</a></div>
      </div>
      <div class="module-grid">${d.modules.filter((m) => m.scope === 'global').map(moduleCard).join('')}</div>
    </section>

    <section class="two-col">
      <div class="card">
        <div class="card-head">
          <h2>Live events</h2>
          <span class="sub" id="feed-state">streaming</span>
          <div class="card-actions"><a class="btn btn-ghost" href="#/events">all events</a></div>
        </div>
        <div class="feed" id="mini-feed">${d.events.map(eventRow).join('') || empty('🌙', 'no events yet')}</div>
      </div>

      <div class="card">
        <div class="card-head"><h2>Quick actions</h2></div>
        <div class="btn-row">
          <button class="btn btn-primary" data-action="broadcast">📣 Broadcast…</button>
          <button class="btn" data-action="run" data-name="sweep">🧹 Sweep now</button>
          <button class="btn" data-action="run" data-name="backup">💾 Backup</button>
          <button class="btn" data-action="run" data-name="purge_events">🗑 Prune events</button>
          <button class="btn btn-ghost" data-action="toggle-maintenance" data-on="${d.modules.find((m) => m.key === 'maintenance')?.enabled ? '1' : '0'}">
            🛠 Maintenance
          </button>
          <button class="btn btn-danger" data-action="lifecycle" data-name="restart">♻️ Restart bot</button>
        </div>
        <p class="muted" style="margin:12px 0 0;font-size:12.5px">
          restart re-execs the bot process; a host with a restart policy brings it straight back.
        </p>
      </div>
    </section>`;
}

function pageModules() {
  const d = App.data.overview;
  if (!d) return skeleton();
  const cats = d.categories || [];
  return `
    <section class="card">
      <div class="card-head">
        <h2>Plugin registry</h2>
        <span class="sub">${d.modules.length} modules · ${d.modules.filter((m) => m.enabled).length} on</span>
      </div>
      <p class="muted" style="margin:0;font-size:13px">
        Global switches apply to the whole bot. Per-group modules are set inside each group
        (or from the Groups page here).
      </p>
    </section>
    ${cats.map((cat) => {
      const mods = d.modules.filter((m) => m.category === cat.key);
      if (!mods.length) return '';
      return `
        <section class="card">
          <div class="section-title"><h2>${esc(cat.label)}</h2><p>${esc(cat.blurb)}</p></div>
          <div class="module-grid" style="margin-top:14px">${mods.map(moduleCard).join('')}</div>
        </section>`;
    }).join('')}`;
}

function pageGroups() {
  const d = App.data.groups;
  if (!d) return skeleton();
  if (!d.groups.length) {
    return empty('💬', d.total ? 'no group matches that search' : 'not in any group yet',
      'add the bot to a group and it shows up here instantly');
  }
  return `
    <section class="card">
      <div class="card-head">
        <h2>Groups</h2>
        <span class="sub">${d.count} shown · ${d.total} total</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr><th>group</th><th>chat id</th><th>modules</th><th class="num">warnings</th><th class="num">filters</th><th>added</th></tr>
          </thead>
          <tbody>
            ${d.groups.map((g) => `
              <tr data-action="open-group" data-chat="${esc(g.chat_id)}">
                <td>
                  <strong>${esc(g.title)}</strong>
                  ${g.active ? '' : '<span class="pill off">inactive</span>'}
                </td>
                <td class="mono">${esc(g.chat_id)}</td>
                <td>
                  <div style="display:flex;align-items:center;gap:9px">
                    <div class="bar" style="flex:1"><i style="width:${Math.round((g.modules_on / Math.max(1, g.modules_total)) * 100)}%"></i></div>
                    <span class="muted" style="font-size:12px">${g.modules_on}/${g.modules_total}</span>
                  </div>
                </td>
                <td class="num">${num(g.warnings)}</td>
                <td class="num">${num(g.filters)}</td>
                <td class="muted">${esc(String(g.added_at).slice(0, 10))}</td>
              </tr>`).join('')}
          </tbody>
        </table>
      </div>
    </section>`;
}

function pageEvents() {
  const d = App.data.events;
  if (!d) return skeleton();
  return `
    <section class="card">
      <div class="card-head">
        <h2>Event log</h2>
        <span class="sub">last 7 days</span>
        <div class="card-actions">
          <button class="btn btn-ghost" id="pause-btn">${App.ui.paused ? '▶ resume' : '⏸ pause'}</button>
          <button class="btn btn-ghost" data-action="export-events">⬇ csv</button>
        </div>
      </div>
      <div class="tags" style="margin-bottom:14px">
        <button class="tag ${App.ui.tag ? '' : 'active'}" data-tag="">everything <b>${num(d.total)}</b></button>
        ${(d.tags || []).map((t) => `
          <button class="tag ${App.ui.tag === t.tag ? 'active' : ''}" data-tag="${esc(t.tag)}">${tagIcon(t.tag)} ${esc(t.tag)} <b>${num(t.count)}</b></button>`).join('')}
      </div>
      <div class="feed" id="feed">${d.events.map(eventRow).join('') || empty('🌙', 'nothing logged yet')}</div>
    </section>`;
}

function pageSettings() {
  const d = App.data.settings;
  if (!d) return skeleton();
  return `
    <section class="two-col">
      <div class="card">
        <div class="card-head"><h2>Configuration</h2><span class="sub">read-only, from the environment</span></div>
        <div class="kv">
          ${d.settings.map((row) => kvRow(`${row.icon} ${row.label}`, esc(row.value))).join('')}
        </div>
      </div>

      <div class="card">
        <div class="card-head"><h2>Broadcast</h2></div>
        <label class="field">
          <span>message to every user</span>
          <textarea id="broadcast-text" placeholder="hello everyone — GOHAN got an upgrade ✨"></textarea>
        </label>
        <div class="btn-row">
          <button class="btn btn-primary" data-action="broadcast">📣 Send</button>
          <button class="btn btn-ghost" data-action="logout">🚪 Sign out</button>
        </div>
        <p class="muted" style="font-size:12.5px;margin:14px 0 0">
          respects Telegram's rate limits: 25 messages, then a short pause.
        </p>
      </div>
    </section>

    ${d.problems?.length ? `
      <section class="card">
        <div class="card-head"><h2>Configuration notes</h2><span class="sub">${d.problems.length}</span></div>
        <div class="feed">${d.problems.map((p) => `
          <div class="event"><div class="event-ico">ℹ️</div>
            <div class="event-main"><strong>heads-up</strong><p>${esc(p)}</p></div></div>`).join('')}</div>
      </section>` : ''}

    <section class="card" style="border-color:color-mix(in srgb, var(--danger) 35%, var(--line))">
      <div class="card-head"><h2>Danger zone</h2><span class="sub">irreversible</span></div>
      <div class="btn-row">
        <button class="btn" data-action="run" data-name="purge_events">🗑 Prune events older than 30 days</button>
        <button class="btn btn-danger" data-action="lifecycle" data-name="restart">♻️ Restart the bot</button>
        <button class="btn btn-danger" data-action="lifecycle" data-name="shutdown">⏹ Shut the bot down</button>
      </div>
      <p class="muted" style="font-size:12.5px;margin:14px 0 0">
        shutting down sends a last message to the owner before the process exits.
      </p>
    </section>`;
}

const kvRow = (label, value) => `<div class="kv-row"><span>${label}</span><span>${value}</span></div>`;
const skeleton = () => `
  <section class="kpis">${'<div class="skeleton" style="height:106px"></div>'.repeat(6)}</section>
  <div class="skeleton" style="height:230px"></div>
  <div class="skeleton" style="height:180px"></div>`;

/* ------------------------------------------------------------ rendering --- */
const PAGES = {
  overview: { title: 'Overview', sub: 'everything the bot is doing, live', render: pageOverview, search: false },
  moderation: { title: 'Moderation', sub: 'cases, offenders and the busiest groups', render: pageModeration, search: false },
  users: { title: 'Users', sub: 'look anyone up and act', render: pageUsers, search: true },
  modules: { title: 'Modules', sub: 'every switch in one place', render: pageModules, search: true },
  groups: { title: 'Groups', sub: 'per-group control', render: pageGroups, search: true },
  events: { title: 'Events', sub: 'the log channel, in a browser', render: pageEvents, search: true },
  settings: { title: 'Settings', sub: 'configuration, broadcast and danger zone', render: pageSettings, search: false },
};

function render() {
  const page = PAGES[App.page] || PAGES.overview;
  $('#page-title').textContent = page.title;
  $('#page-sub').textContent = page.sub;
  $$('.nav-item').forEach((el) => el.classList.toggle('active', el.dataset.page === App.page));
  const search = $('#search-wrap');
  search.hidden = !page.search;
  $('#view').innerHTML = page.render();
  if (App.page === 'overview' && App.data.overview) {
    const canvas = $('#chart');
    if (canvas) drawChart(canvas, App.data.overview.activity);
  }
  if (App.page === 'moderation' && App.data.moderation) {
    const canvas = $('#mod-chart');
    if (canvas) drawChart(canvas, App.data.moderation.chart);
  }
  updateBadges();
}

function updateBadges() {
  const d = App.data.overview;
  if (!d) return;
  $('#badge-modules').textContent = `${d.modules.filter((m) => m.enabled).length}`;
  $('#badge-groups').textContent = `${d.counts?.groups ?? ''}`;
  const warningsBadge = $('#badge-warnings');
  if (warningsBadge) warningsBadge.textContent = num(d.counts?.warnings ?? 0);
  $('#badge-events').textContent = '';
  $('#brand-mode').textContent = d.demo ? 'demo mode' : 'live control panel';
  $('#demo-chip').hidden = !d.demo;
  const status = $('#status-dot');
  status.className = `dot ${d.demo ? 'warn' : 'ok'}`;
  $('#status-text').textContent = d.demo ? 'demo data' : 'connected';
  $('#status-sub').textContent = `v${document.body.dataset.version} · ${d.bot?.host || ''}`;
}

/* ------------------------------------------------------------- loading ---- */
async function loadOverview() {
  App.data.overview = await api('/api/overview');
  updateBadges();
}
async function loadGroups(query = App.ui.groupQuery) {
  App.data.groups = await api(`/api/groups?q=${encodeURIComponent(query)}&limit=100`);
}
async function loadEvents({ quiet = false } = {}) {
  const query = App.ui.search;
  App.data.events = await api(`/api/events?limit=80&tag=${encodeURIComponent(App.ui.tag)}&q=${encodeURIComponent(query)}`);
  if (!quiet && App.page === 'events') render();
}
async function loadSettings() {
  App.data.settings = await api('/api/settings');
}
async function loadModeration(days = App.ui.modDays) {
  App.data.moderation = await api(`/api/moderation?days=${encodeURIComponent(days)}&limit=60`);
}
async function loadUsers(query = App.ui.userQuery, filter = App.ui.userFilter) {
  const banned = filter === 'banned' ? 'banned=true' : '';
  App.data.users = await api(`/api/users?q=${encodeURIComponent(query)}&${banned}&limit=60`);
}

async function refresh({ silent = false } = {}) {
  try {
    if (App.page === 'overview' || !App.data.overview) await loadOverview();
    if (App.page === 'groups') await loadGroups();
    if (App.page === 'events' && !App.ui.paused) await loadEvents({ quiet: true });
    if (App.page === 'settings') await loadSettings();
    if (App.page === 'moderation') await loadModeration();
    if (App.page === 'users') await loadUsers();
    if (!silent) render();
    else if (App.page === 'overview') render();
  } catch (error) {
    if (error.authMissing) return showAuth();
    toast('err', 'refresh failed', error.message);
  }
}

/* ---------------------------------------------------------------- live ---- */
function startStream() {
  stopStream();
  const source = new EventSource('/api/stream');
  App.sse = source;
  source.onmessage = (event) => {
    App.retry = 0;
    $('#live-pill').classList.remove('paused');
    try {
      const payload = JSON.parse(event.data);
      if (App.page === 'overview' && App.data.overview) {
        const feed = $('#mini-feed');
        if (feed) feed.innerHTML = payload.events.map(eventRow).join('') || empty('🌙', 'no events yet');
        const state = $('#feed-state');
        if (state) state.textContent = `streaming · ${clock(payload.ts)}`;
      }
      if (App.page === 'events' && !App.ui.paused) {
        const feed = $('#feed');
        if (feed) feed.innerHTML = payload.events.map(eventRow).join('') || empty('🌙', 'nothing logged yet');
      }
    } catch { /* ignore malformed frame */ }
  };
  source.onerror = () => {
    source.close();
    App.sse = null;
    App.retry += 1;
    $('#live-pill').classList.add('paused');
    if (App.retry <= 1) toast('err', 'live stream lost', 'falling back to polling');
    // poll instead, with a light backoff, and retry the stream later
    if (!App.poll) App.poll = setInterval(() => refresh({ silent: true }), 15000);
    setTimeout(startStream, Math.min(60000, 4000 * App.retry));
  };
  if (App.poll) { clearInterval(App.poll); App.poll = null; }
}
function stopStream() {
  if (App.sse) { App.sse.close(); App.sse = null; }
  if (App.poll) { clearInterval(App.poll); App.poll = null; }
}

/* -------------------------------------------------------------- actions --- */
async function toggleModule(input) {
  const key = input.dataset.key;
  const scope = input.dataset.scope;
  const chat = input.dataset.chat;
  const enabled = input.checked;
  const wrapper = input.closest('.switch');
  wrapper?.classList.add('busy');
  try {
    if (scope === 'global') {
      await api(`/api/modules/${encodeURIComponent(key)}`, { method: 'POST', body: { enabled } });
    } else {
      await api(`/api/groups/${encodeURIComponent(chat)}/modules/${encodeURIComponent(key)}`, {
        method: 'POST', body: { enabled },
      });
    }
    const list = App.data.overview?.modules || [];
    const found = list.find((m) => m.key === key);
    if (found) found.enabled = enabled;
    input.closest('.module')?.classList.toggle('on', enabled);
    toast('ok', `${key} ${enabled ? 'enabled' : 'disabled'}`);
  } catch (error) {
    input.checked = !enabled;
    toast('err', 'could not change it', error.message);
  } finally {
    wrapper?.classList.remove('busy');
  }
}

async function openUser(userId) {
  openDrawer('loading…', `<div class="skeleton" style="height:200px"></div>`);
  try {
    const data = await api(`/api/users/${encodeURIComponent(userId)}`);
    const u = data.user;
    openDrawer(u.name, `
      <div class="kv">
        ${kvRow('🆔 user id', `<span class="mono">${esc(u.user_id)}</span>`)}
        ${kvRow('🔗 username', u.username ? `@${esc(u.username)}` : '—')}
        ${kvRow('⚠️ warnings', num(u.warnings))}
        ${kvRow('⭐ premium', u.premium ? 'yes' : 'no')}
        ${kvRow('📅 first seen', esc(String(u.first_seen).slice(0, 10)))}
        ${kvRow('👀 last seen', esc(String(u.last_seen).slice(0, 10)))}
        ${kvRow('🚫 status', u.banned ? '<span class="pill bad">blocked</span>' : '<span class="pill ok">allowed</span>')}
      </div>

      <div class="btn-row">
        <button class="btn ${u.banned ? 'btn-success' : 'btn-danger'}"
          data-action="user-action" data-user="${esc(u.user_id)}" data-op="${u.banned ? 'unban' : 'ban'}">
          ${u.banned ? '✅ Unblock user' : '🚫 Block user'}
        </button>
        <button class="btn" data-action="user-action" data-user="${esc(u.user_id)}" data-op="clear_warnings">
          🧹 Clear warnings
        </button>
      </div>

      ${data.warnings?.length ? `
        <h3 style="font-size:13px;color:var(--mut);text-transform:uppercase;letter-spacing:.8px">warnings</h3>
        <div class="feed">
          ${data.warnings.map((w) => `
            <div class="event">
              <div class="event-ico">⚠️</div>
              <div class="event-main"><strong>${esc(w.reason)}</strong><p>${esc(w.chat_title || w.chat_id)} · by <span class="mono">${esc(w.admin_id ?? '—')}</span></p></div>
              <div class="event-time">${esc(w.age)}</div>
            </div>`).join('')}
        </div>` : ''}

      ${data.groups?.length ? `
        <h3 style="font-size:13px;color:var(--mut);text-transform:uppercase;letter-spacing:.8px">groups</h3>
        <div class="kv">${data.groups.map((g) => kvRow(`💬 ${esc(g.title)}`, `${num(g.warnings)} warnings`)).join('')}</div>` : ''}

      ${data.scores?.length ? `
        <h3 style="font-size:13px;color:var(--mut);text-transform:uppercase;letter-spacing:.8px">best scores</h3>
        <div class="kv">${data.scores.map((sc) => kvRow(`🎮 ${esc(sc.game)}`, num(sc.score))).join('')}</div>` : ''}
    `);
  } catch (error) {
    closeDrawer();
    toast('err', 'could not open that user', error.message);
  }
}

async function openGroup(chatId) {
  openDrawer('loading…', `<div class="skeleton" style="height:200px"></div>`);
  try {
    const data = await api(`/api/groups/${encodeURIComponent(chatId)}`);
    const g = data.group;
    const modules = App.data.overview?.modules.filter((m) => m.scope === 'chat') || [];
    const drawerHtml = `
      <div class="kv">
        ${kvRow('🆔 chat id', `<span class="mono">${esc(g.chat_id)}</span>`)}
        ${kvRow('📦 type', esc(g.type))}
        ${kvRow('✅ active', g.active ? '<span class="pill ok">yes</span>' : '<span class="pill off">no</span>')}
        ${kvRow('⚠️ warnings', num(g.warnings))}
        ${kvRow('🧲 filters', num(g.filters))}
        ${kvRow('📝 notes', num(g.notes))}
      </div>

      <h3 style="font-size:13px;color:var(--mut);text-transform:uppercase;letter-spacing:.8px">modules</h3>
      <div class="module-grid" style="grid-template-columns:1fr">
        ${modules.map((m) => `
          <article class="module ${g.module_states?.[m.key] ? 'on' : ''}">
            <div class="module-ico">${esc(m.icon)}</div>
            <div class="module-body"><strong>${esc(m.name)}</strong><p>${esc(m.blurb)}</p></div>
            ${switchHtml(m.key, Boolean(g.module_states?.[m.key]), { scope: 'chat', chat: g.chat_id })}
          </article>`).join('')}
      </div>

      ${g.top_filters?.length ? `
        <h3 style="font-size:13px;color:var(--mut);text-transform:uppercase;letter-spacing:.8px">top filters</h3>
        <div class="kv">${g.top_filters.map((f) => kvRow(esc(f.trigger), `${num(f.uses)} uses`)).join('')}</div>` : ''}

      <h3 style="font-size:13px;color:var(--mut);text-transform:uppercase;letter-spacing:.8px">edit this group</h3>
      <label class="field">
        <span>welcome message</span>
        <textarea id="g-welcome" rows="3" placeholder="👋 welcome {name} to {chat}!"></textarea>
      </label>
      <label class="field">
        <span>rules (/rules)</span>
        <textarea id="g-rules" rows="4" placeholder="1. be kind&#10;2. no spam"></textarea>
      </label>
      <label class="field" style="max-width:180px">
        <span>warnings before a ban</span>
        <input id="g-warnlimit" type="number" min="1" max="20" value="${esc(g.editable?.warn_limit ?? 3)}">
      </label>
      <div class="btn-row">
        <button class="btn btn-primary" data-action="save-group" data-chat="${esc(g.chat_id)}">💾 Save group settings</button>
      </div>

      ${g.top_warned?.length ? `
        <h3 style="font-size:13px;color:var(--mut);text-transform:uppercase;letter-spacing:.8px">most warned</h3>
        <div class="kv">${g.top_warned.map((w) => kvRow(`<span class="mono">${esc(w.user_id)}</span>`, `${num(w.warnings)} warnings`)).join('')}</div>` : ''}
    `;
    openDrawer(g.title, drawerHtml);
    const welcome = $('#g-welcome');
    const rules = $('#g-rules');
    if (welcome) welcome.value = g.editable?.welcome_text || '';
    if (rules) rules.value = g.editable?.rules || '';
  } catch (error) {
    closeDrawer();
    toast('err', 'could not open that group', error.message);
  }
}

async function saveGroupSettings(chatId) {
  const settings = {
    welcome_text: $('#g-welcome')?.value ?? '',
    rules: $('#g-rules')?.value ?? '',
    warn_limit: Number($('#g-warnlimit')?.value || 3),
  };
  try {
    const result = await api(`/api/groups/${encodeURIComponent(chatId)}/settings`, {
      method: 'PATCH', body: { settings },
    });
    toast('ok', 'group settings saved', Object.keys(result.updated || {}).join(', '));
  } catch (error) {
    toast('err', 'could not save', error.message);
  }
}

async function userAction(userId, op) {
  try {
    const result = await api(`/api/users/${encodeURIComponent(userId)}/actions/${encodeURIComponent(op)}`, {
      method: 'POST', body: {},
    });
    toast('ok', result.message || op, result.demo ? 'demo no-op' : '');
    closeDrawer();
    if (App.page === 'users') await loadUsers();
    if (App.page === 'moderation') await loadModeration();
    render();
  } catch (error) {
    toast('err', `${op} failed`, error.message);
  }
}

async function applyModuleAll(key, enabled) {
  try {
    const result = await api(`/api/modules/${encodeURIComponent(key)}/apply-all`, {
      method: 'POST', body: { enabled },
    });
    toast('ok', `${key} → ${enabled ? 'on' : 'off'}`, `applied to ${result.groups} group(s)`);
  } catch (error) {
    toast('err', 'bulk change failed', error.message);
  }
}

async function runAction(name, body = {}) {
  try {
    const result = await api(`/api/actions/${encodeURIComponent(name)}`, { method: 'POST', body });
    toast('ok', name, result.message || 'done');
    if (name === 'backup' && result.size_kb !== undefined) {
      toast('ok', 'backup ready', `${result.size_kb} kb in ${result.database || 'data/'}`);
    }
    return result;
  } catch (error) {
    toast('err', `${name} failed`, error.message);
    return null;
  }
}

function askBroadcast() {
  showModal({
    title: 'Send a broadcast',
    body: 'Goes to every known user in a private chat, rate limited.',
    confirmLabel: '📣 Send now',
    input: { label: 'message', placeholder: 'hello everyone — GOHAN got an upgrade ✨' },
    onConfirm: async (text) => {
      if (!text?.trim()) return toast('err', 'nothing to send');
      const result = await runAction('broadcast', { text });
      return result;
    },
  });
}

function askLifecycle(name) {
  const isStop = name === 'shutdown';
  showModal({
    title: isStop ? 'Shut the bot down?' : 'Restart the bot?',
    body: isStop
      ? 'The bot sends a final message to its owner and then exits. On a host with a restart policy it will come back on its own.'
      : 'The process re-execs itself. Downtime is a couple of seconds.',
    confirmLabel: isStop ? '⏹ Shut down' : '♻️ Restart',
    danger: true,
    onConfirm: async () => {
      const result = await runAction(name);
      if (result?.demo) toast('warn', 'demo mode', 'lifecycle actions are disabled here');
    },
  });
}

/* ------------------------------------------------------------- palette ---- */
function paletteItems() {
  const items = [
    { icon: '📊', label: 'Overview', hint: 'page', run: () => go('overview') },
    { icon: '🧩', label: 'Modules', hint: 'page', run: () => go('modules') },
    { icon: '💬', label: 'Groups', hint: 'page', run: () => go('groups') },
    { icon: '🛡', label: 'Moderation', hint: 'page', run: () => go('moderation') },
    { icon: '👥', label: 'Users', hint: 'page', run: () => go('users') },
    { icon: '📨', label: 'Events', hint: 'page', run: () => go('events') },
    { icon: '⚙️', label: 'Settings', hint: 'page', run: () => go('settings') },
    { icon: '📣', label: 'Broadcast a message', hint: 'action', run: askBroadcast },
    { icon: '🧹', label: 'Run a watchdog sweep', hint: 'action', run: () => runAction('sweep') },
    { icon: '💾', label: 'Back up the database', hint: 'action', run: () => runAction('backup') },
    { icon: '🛠', label: 'Toggle maintenance mode', hint: 'action', run: toggleMaintenance },
    { icon: '♻️', label: 'Restart the bot', hint: 'danger', run: () => askLifecycle('restart') },
    { icon: '⏹', label: 'Shut the bot down', hint: 'danger', run: () => askLifecycle('shutdown') },
    { icon: '🌗', label: 'Switch theme', hint: 'ui', run: toggleTheme },
  ];
  (App.data.groups?.groups || []).slice(0, 40).forEach((g) => {
    items.push({ icon: '💬', label: g.title, hint: 'group', run: () => openGroup(g.chat_id) });
  });
  return items;
}

function openPalette() {
  const palette = $('#palette');
  palette.hidden = false;
  const input = $('#palette-input');
  input.value = '';
  App.ui.selected = 0;
  drawPalette('');
  setTimeout(() => input.focus(), 30);
}
function closePalette() { $('#palette').hidden = true; }

function drawPalette(query) {
  const needle = query.trim().toLowerCase();
  const items = paletteItems().filter((i) => !needle || i.label.toLowerCase().includes(needle));
  App.ui.paletteItems = items;
  App.ui.selected = Math.min(App.ui.selected, Math.max(0, items.length - 1));
  $('#palette-list').innerHTML = items.length
    ? items.map((item, index) => `
        <li class="${index === App.ui.selected ? 'sel' : ''}" data-index="${index}">
          <span>${item.icon}</span>${esc(item.label)}<small>${esc(item.hint)}</small>
        </li>`).join('')
    : `<li class="muted">nothing matches “${esc(query)}”</li>`;
}

function runPalette(index) {
  const item = App.ui.paletteItems?.[index];
  if (!item) return;
  closePalette();
  item.run();
}

/* --------------------------------------------------------------- shell ---- */
function go(page) {
  App.page = page;
  location.hash = `#/${page}`;
  render();
  refresh({ silent: false });
}

function toggleTheme() {
  const root = document.documentElement;
  const next = root.dataset.theme === 'light' ? 'dark' : 'light';
  root.dataset.theme = next;
  localStorage.setItem('gohan-theme', next);
  if (App.page === 'overview') render();
}

async function toggleMaintenance() {
  const current = App.data.overview?.modules.find((m) => m.key === 'maintenance');
  const enabled = !current?.enabled;
  const result = await api('/api/modules/maintenance', { method: 'POST', body: { enabled } }).catch((e) => {
    toast('err', 'maintenance toggle failed', e.message);
    return null;
  });
  if (result) {
    if (current) current.enabled = enabled;
    toast('ok', enabled ? 'maintenance on' : 'maintenance off', enabled ? 'regular users are paused' : 'everyone is welcome again');
    render();
  }
}

function exportEvents() {
  const rows = App.data.events?.events || [];
  if (!rows.length) return toast('err', 'nothing to export');
  const csv = ['time,tag,chat_id,user_id,data']
    .concat(rows.map((e) => [e.ts, e.tag, e.chat_id ?? '', e.user_id ?? '', `"${String(e.data ?? '').replace(/"/g, '""')}"`].join(',')))
    .join('\n');
  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = `gohan-events-${new Date().toISOString().slice(0, 10)}.csv`;
  link.click();
  URL.revokeObjectURL(url);
  toast('ok', 'exported', `${rows.length} events`);
}

/* ------------------------------------------------------------ listeners --- */
function wire() {
  // navigation + delegated actions
  document.addEventListener('click', async (event) => {
    const nav = event.target.closest('.nav-item');
    if (nav) { event.preventDefault(); go(nav.dataset.page); return; }

    const tag = event.target.closest('[data-tag]');
    if (tag) {
      App.ui.tag = tag.dataset.tag || '';
      await loadEvents();
      render();
      return;
    }

    const groupRow = event.target.closest('[data-action="open-group"]');
    if (groupRow) { openGroup(groupRow.dataset.chat); return; }

    const userRow = event.target.closest('[data-action="open-user"]');
    if (userRow) { openUser(userRow.dataset.user); return; }

    const action = event.target.closest('[data-action]');
    if (action) {
      const name = action.dataset.action;
      if (name === 'broadcast') askBroadcast();
      else if (name === 'run') await runAction(action.dataset.name);
      else if (name === 'open-user') openUser(action.dataset.user);
      else if (name === 'user-action') await userAction(action.dataset.user, action.dataset.op);
      else if (name === 'save-group') await saveGroupSettings(action.dataset.chat);
      else if (name === 'apply-all') await applyModuleAll(action.dataset.key, action.dataset.on !== '1');
      else if (name === 'user-filter') { App.ui.userFilter = action.dataset.filter; await loadUsers(); render(); }
      else if (name === 'window') { App.ui.modDays = Number(action.dataset.days); await loadModeration(); render(); }
      else if (name === 'lifecycle') askLifecycle(action.dataset.name);
      else if (name === 'toggle-maintenance') await toggleMaintenance();
      else if (name === 'logout') {
        await api('/api/auth/logout', { method: 'POST' }).catch(() => {});
        location.reload();
      } else if (name === 'export-events') exportEvents();
      return;
    }

    if (event.target.closest('#drawer') && !event.target.closest('.drawer-panel')) closeDrawer();
    if (event.target.closest('#modal') && !event.target.closest('.modal-card')) closeModal();
    if (event.target.closest('#palette') && !event.target.closest('.palette-card')) closePalette();
  });

  // module switches
  document.addEventListener('change', (event) => {
    const input = event.target.closest('[data-toggle="module"]');
    if (input) toggleModule(input);
    const broadcast = event.target.closest('#broadcast-text');
    if (broadcast) localStorage.setItem('gohan-broadcast', broadcast.value);
  });

  $('#refresh-btn').onclick = () => { refresh({ silent: false }); toast('ok', 'refreshed'); };
  $('#theme-btn').onclick = toggleTheme;
  $('#palette-btn').onclick = openPalette;
  $('#drawer-close').onclick = closeDrawer;
  $('#side-open')?.addEventListener('click', () => $('#side').classList.add('open'));
  $('#side-close')?.addEventListener('click', () => $('#side').classList.remove('open'));

  const search = $('#search');
  search.addEventListener('input', debounce(async (event) => {
    App.ui.search = event.target.value;
    if (App.page === 'groups') { await loadGroups(App.ui.search); render(); }
    else if (App.page === 'events') { await loadEvents(); }
    else if (App.page === 'users') { App.ui.userQuery = App.ui.search; await loadUsers(); render(); }
  }, 260));

  document.addEventListener('input', debounce((event) => {
    if (event.target.id === 'case-search') {
      App.ui.caseQuery = event.target.value;
      const table = event.target.closest('.card')?.querySelector('tbody');
      if (table && App.data.moderation) {
        table.innerHTML = App.data.moderation.cases.filter(matchCase).map(caseRow).join('')
          || `<tr><td colspan="5">${empty('🌙', 'no cases match')}</td></tr>`;
      }
    }
  }, 200));

  document.addEventListener('click', (event) => {
    if (event.target.closest('#pause-btn')) {
      App.ui.paused = !App.ui.paused;
      render();
      toast('ok', App.ui.paused ? 'feed paused' : 'feed resumed');
    }
  });

  const paletteInput = $('#palette-input');
  paletteInput.addEventListener('input', (event) => { App.ui.selected = 0; drawPalette(event.target.value); });
  paletteInput.addEventListener('keydown', (event) => {
    if (event.key === 'ArrowDown') { App.ui.selected = Math.min((App.ui.paletteItems?.length || 1) - 1, App.ui.selected + 1); drawPalette(paletteInput.value); event.preventDefault(); }
    else if (event.key === 'ArrowUp') { App.ui.selected = Math.max(0, App.ui.selected - 1); drawPalette(paletteInput.value); event.preventDefault(); }
    else if (event.key === 'Enter') { runPalette(App.ui.selected); event.preventDefault(); }
  });
  $('#palette-list').addEventListener('click', (event) => {
    const li = event.target.closest('li[data-index]');
    if (li) runPalette(Number(li.dataset.index));
  });

  document.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); openPalette(); return; }
    if (event.key === 'Escape') { closePalette(); closeModal(); closeDrawer(); }
    if (event.key === '/' && !['INPUT', 'TEXTAREA'].includes(document.activeElement.tagName)) {
      event.preventDefault();
      ($('#search') || $('#palette-input')).focus();
    }
    if (event.key === 'r' && (event.ctrlKey || event.metaKey) && event.shiftKey) { event.preventDefault(); refresh(); }
  });

  window.addEventListener('resize', debounce(() => {
    if (App.page === 'overview' && App.data.overview) {
      const canvas = $('#chart');
      if (canvas) drawChart(canvas, App.data.overview.activity);
    }
  }, 150));

  window.addEventListener('hashchange', () => {
    const page = (location.hash.replace('#/', '') || 'overview').split('/')[0];
    if (PAGES[page] && page !== App.page) { App.page = page; render(); refresh({ silent: false }); }
  });
}

/* ----------------------------------------------------------------- auth --- */
function showAuth() {
  $('#boot').hidden = true;
  $('#app').hidden = true;
  $('#auth').hidden = false;
}

async function boot() {
  const savedTheme = localStorage.getItem('gohan-theme');
  if (savedTheme) document.documentElement.dataset.theme = savedTheme;

  const hash = (location.hash.replace('#/', '') || 'overview').split('/')[0];
  if (PAGES[hash]) App.page = hash;

  let me = null;
  try { me = await api('/api/me'); } catch (error) { if (error.authMissing) return showAuth(); }

  if (me?.auth_required && !me.authenticated) {
    // the login form posts below and reloads
    $('#auth-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const token = $('#auth-token').value.trim();
      try {
        await api('/api/auth/login', { method: 'POST', body: { token } });
        location.reload();
      } catch (error) {
        const box = $('#auth-error');
        box.textContent = error.message;
        box.hidden = false;
      }
    });
    return showAuth();
  }

  $('#boot').hidden = true;
  $('#app').hidden = false;
  wire();
  await refresh({ silent: true });
  render();
  startStream();
  // a slow safety net even when the stream is healthy (covers tab suspension)
  setInterval(() => { if (!App.sse) refresh({ silent: true }); }, 45000);
}

document.addEventListener('DOMContentLoaded', boot);
