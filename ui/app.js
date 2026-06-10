/* Loomlet dashboard — polls GET /api/status every 5s and renders agents + activity.
   All payload values are rendered via textContent, never markup. */
'use strict';

(function () {
  var POLL_MS = 5000;
  var MAX_ROWS = 100;
  var KNOWN_AGENTS = ['coder', 'tester'];

  var KIND_CLASS = {
    fleet_start: 'k-info',
    issue_picked: 'k-info',
    verify_start: 'k-info',
    agent_start: 'k-amber',
    agent_done: 'k-amber',
    pr_opened: 'k-amber',
    gate_green_merged: 'k-green',
    deployed: 'k-green',
    gate_red: 'k-red',
    orchestrator_error: 'k-red'
  };

  var lastData = null;

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function relTime(value) {
    var t = Date.parse(String(value));
    if (isNaN(t)) return '';
    var s = Math.floor((Date.now() - t) / 1000);
    if (s < 0) return '';
    if (s < 60) return s + 's ago';
    var m = Math.floor(s / 60);
    if (m < 60) return m + 'm ago';
    var h = Math.floor(m / 60);
    if (h < 48) return h + 'h ago';
    return Math.floor(h / 24) + 'd ago';
  }

  function safeIdPart(name) {
    return String(name).toLowerCase().replace(/[^a-z0-9_-]/g, '-');
  }

  function renderAgents(agents) {
    var grid = document.getElementById('agents');
    grid.replaceChildren();

    var names = KNOWN_AGENTS.slice();
    Object.keys(agents || {}).forEach(function (name) {
      if (names.indexOf(name) === -1) names.push(name);
    });

    var rendered = 0;
    names.forEach(function (name) {
      var info = (agents && agents[name]) || null;
      if (!info && KNOWN_AGENTS.indexOf(name) === -1) return;

      var state = info && info.state ? String(info.state) : 'unknown';
      var card = el('article', 'agent-card state-' + safeIdPart(state));
      card.id = 'agent-' + safeIdPart(name);

      var head = el('div', 'agent-head');
      head.appendChild(el('span', 'agent-name', name));
      head.appendChild(el('span', 'state-badge', state));
      card.appendChild(head);

      card.appendChild(el('p', 'agent-detail',
        info && info.detail ? String(info.detail) : '—'));

      var since = el('div', 'agent-since');
      var sinceRaw = info && info.since ? String(info.since) : '';
      since.appendChild(el('span', null, 'since ' + (sinceRaw || '—')));
      var rel = relTime(sinceRaw);
      if (rel) {
        since.appendChild(document.createTextNode(' · '));
        since.appendChild(el('span', 'rel', rel));
      }
      card.appendChild(since);

      grid.appendChild(card);
      rendered += 1;
    });

    if (rendered === 0) {
      grid.appendChild(el('div', 'grid-empty', 'no fleet data yet'));
    }
  }

  function renderEvents(events) {
    var feed = document.getElementById('activity');
    feed.replaceChildren();

    var rows = Array.isArray(events) ? events.slice() : [];
    // Server already sends newest-first; a stable sort on ts keeps that
    // guarantee even if the payload ever arrives unordered.
    rows.sort(function (a, b) {
      var ta = a && a.ts ? String(a.ts) : '';
      var tb = b && b.ts ? String(b.ts) : '';
      if (ta < tb) return 1;
      if (ta > tb) return -1;
      return 0;
    });
    rows = rows.slice(0, MAX_ROWS);

    if (rows.length === 0) {
      feed.appendChild(el('div', 'feed-empty', 'no events yet'));
      return;
    }

    rows.forEach(function (evt) {
      if (!evt || typeof evt !== 'object') return;
      var row = el('div', 'evt-row');
      var kind = evt.kind ? String(evt.kind) : '?';

      row.appendChild(el('span', 'evt-ts', evt.ts ? String(evt.ts) : '—'));
      row.appendChild(el('span', 'evt-kind ' + (KIND_CLASS[kind] || 'k-plain'), kind));

      var fields = el('span', 'evt-fields');
      Object.keys(evt).forEach(function (key) {
        if (key === 'ts' || key === 'kind') return;
        var value = evt[key];
        var pair = el('span', null);
        var label = el('b', null, key);
        pair.appendChild(label);
        pair.appendChild(document.createTextNode(
          '=' + (typeof value === 'string' ? value : JSON.stringify(value)) + '  '));
        fields.appendChild(pair);
      });
      row.appendChild(fields);

      feed.appendChild(row);
    });
  }

  function setOnline(online) {
    var banner = document.getElementById('offline-banner');
    var dot = document.getElementById('sync-dot');
    var text = document.getElementById('sync-text');
    banner.hidden = online;
    dot.className = 'dot ' + (online ? 'dot-on' : 'dot-off');
    if (online) {
      text.textContent = 'live · synced ' + new Date().toLocaleTimeString() +
        ' · refreshes every ' + (POLL_MS / 1000) + 's';
    } else {
      text.textContent = 'offline · last attempt ' + new Date().toLocaleTimeString();
    }
  }

  function render(data) {
    renderAgents(data && data.agents);
    renderEvents(data && data.events);
  }

  function tick() {
    fetch('/api/status', { cache: 'no-store' })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (data) {
        lastData = data;
        render(data);
        setOnline(true);
      })
      .catch(function () {
        setOnline(false);
        // Keep showing the last good data, dimmed by the banner; if we never
        // got any, render the empty-state shells so the page is never blank.
        if (!lastData) render(null);
      });
  }

  tick();
  setInterval(tick, POLL_MS);

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) tick();
  });
})();
