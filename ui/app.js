/* Loomlet fleet manager — polls GET /api/status every 5s and renders the
   pipeline diagram, per-project panels, merged agents and activity.
   All payload values are rendered via textContent, never markup. */
'use strict';

(function () {
  var POLL_MS = 5000;
  var MAX_ROWS = 100;
  var MAX_PROJECT_GATES = 25;
  var FLASH_MS = 15000; // how long a gate/deploy event lights its stage
  var KNOWN_AGENTS = ['coder', 'tester'];
  var STAGES = ['planner', 'coder', 'tester', 'gate', 'deploy'];
  var GITHUB_OWNER = 'https://github.com/Nodenester/';

  var KIND_CLASS = {
    fleet_start: 'k-info',
    planner_start: 'k-info',
    issue_created: 'k-info',
    issue_picked: 'k-info',
    verify_start: 'k-info',
    agent_start: 'k-amber',
    agent_done: 'k-amber',
    pr_opened: 'k-amber',
    gate_green_merged: 'k-green',
    deployed: 'k-green',
    project_complete: 'k-green',
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

  function asCount(value) {
    var n = Number(value);
    return isFinite(n) && n >= 0 ? Math.floor(n) : 0;
  }

  function sortNewestFirst(rows) {
    rows.sort(function (a, b) {
      var ta = a && a.ts ? String(a.ts) : '';
      var tb = b && b.ts ? String(b.ts) : '';
      if (ta < tb) return 1;
      if (ta > tb) return -1;
      return 0;
    });
    return rows;
  }

  /* ---------- pipeline diagram ---------- */

  function stageForAgent(key) {
    // Agent keys may be plain ("coder") or namespaced ("project:coder").
    var base = String(key).split(':').pop().toLowerCase();
    if (base.indexOf('planner') !== -1) return 'planner';
    if (base.indexOf('coder') !== -1) return 'coder';
    if (base.indexOf('tester') !== -1) return 'tester';
    return null;
  }

  function renderPipeline(data) {
    var active = {};

    function markWorking(key, info) {
      if (!info || String(info.state) !== 'working') return;
      var stage = stageForAgent(key);
      if (stage) active[stage] = true;
    }

    var merged = (data && data.agents && typeof data.agents === 'object') ? data.agents : {};
    Object.keys(merged).forEach(function (key) { markWorking(key, merged[key]); });

    var projects = (data && data.projects && typeof data.projects === 'object') ? data.projects : {};
    Object.keys(projects).forEach(function (name) {
      var perProject = projects[name] && projects[name].agents;
      if (!perProject || typeof perProject !== 'object') return;
      Object.keys(perProject).forEach(function (key) { markWorking(key, perProject[key]); });
    });

    // Recent gate / deploy events flash their stage for a few refreshes.
    var now = Date.now();
    var events = Array.isArray(data && data.events) ? data.events : [];
    events.forEach(function (evt) {
      if (!evt) return;
      var kind = evt.kind;
      if (kind !== 'gate_green_merged' && kind !== 'gate_red' && kind !== 'deployed') return;
      var t = Date.parse(String(evt.ts || ''));
      if (isNaN(t)) return;
      var age = now - t;
      if (age <= FLASH_MS && age >= -60000) { // tolerate slight clock skew
        active[kind === 'deployed' ? 'deploy' : 'gate'] = true;
      }
    });

    STAGES.forEach(function (stage) {
      var on = !!active[stage];
      var g = document.getElementById('stage-' + stage);
      if (g) g.classList.toggle('active', on);
      // Narrow stacked layout duplicates the stages without ids; keep its
      // highlight in sync so whichever variant is visible reflects the state.
      var dups = document.querySelectorAll('.ps-stage[data-stage="' + stage + '"]');
      for (var i = 0; i < dups.length; i++) dups[i].classList.toggle('active', on);
    });
  }

  /* ---------- project panels ---------- */

  function gateEntry(evt, repoName) {
    var isGreen = evt.kind === 'gate_green_merged';
    var entry = el('article', 'gate-entry ' + (isGreen ? 'gate-green' : 'gate-red'));

    var head = el('div', 'gate-head');
    head.appendChild(el('span', 'gate-verdict', isGreen ? 'merged' : 'rejected'));

    var prNum = evt.pr !== undefined && evt.pr !== null ? String(evt.pr) : '';
    if (/^\d+$/.test(prNum)) {
      var link = el('a', 'gate-pr', 'PR #' + prNum);
      link.href = GITHUB_OWNER + encodeURIComponent(String(repoName)) + '/pull/' + prNum;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      head.appendChild(link);
    } else if (prNum) {
      head.appendChild(el('span', 'gate-pr', 'PR ' + prNum));
    }

    head.appendChild(el('span', 'gate-ts', evt.ts ? String(evt.ts) : '—'));
    entry.appendChild(head);

    if (!isGreen) {
      var reasons = Array.isArray(evt.reasons) ? evt.reasons : [];
      if (reasons.length > 0) {
        var ul = el('ul', 'gate-reasons');
        reasons.forEach(function (reason) {
          ul.appendChild(el('li', null,
            typeof reason === 'string' ? reason : JSON.stringify(reason)));
        });
        entry.appendChild(ul);
      } else {
        entry.appendChild(el('p', 'gate-no-reasons', 'no reasons recorded'));
      }
    }

    return entry;
  }

  function projectGateEvents(project, name, mergedEvents) {
    var source = Array.isArray(project && project.events)
      ? project.events
      : (Array.isArray(mergedEvents) ? mergedEvents : []).filter(function (evt) {
          return evt && String(evt.project || '') === name;
        });
    var rows = source.filter(function (evt) {
      return evt && (evt.kind === 'gate_green_merged' || evt.kind === 'gate_red');
    });
    return sortNewestFirst(rows.slice());
  }

  function agentRow(name, info) {
    var state = info && info.state ? String(info.state) : 'unknown';
    var row = el('div', 'pa-row state-' + safeIdPart(state));
    row.appendChild(el('span', 'pa-name', name));
    row.appendChild(el('span', 'state-badge', state));
    if (info && info.detail) row.appendChild(el('span', 'pa-detail', String(info.detail)));
    return row;
  }

  function renderProjects(data) {
    var box = document.getElementById('projects');
    box.replaceChildren();

    var projects = (data && data.projects && typeof data.projects === 'object') ? data.projects : {};
    var names = Object.keys(projects).sort();

    if (names.length === 0) {
      box.appendChild(el('div', 'grid-empty', 'no projects registered yet'));
      return;
    }

    names.forEach(function (name) {
      var p = projects[name] || {};
      var card = el('article', 'project-card');
      card.id = 'project-' + safeIdPart(name);

      var head = el('div', 'project-head');
      head.appendChild(el('h3', 'project-name', name));
      if (p.complete) head.appendChild(el('span', 'badge-complete', 'complete'));
      card.appendChild(head);

      card.appendChild(el('p', 'project-goal',
        p.goal ? String(p.goal) : '— no goal recorded —'));

      var done = asCount(p.done);
      var total = asCount(p.total);
      var progress = el('div', 'project-progress');
      progress.appendChild(el('span', 'progress-text',
        done + ' / ' + total + ' issues done'));
      var bar = el('div', 'bar');
      var fill = el('div', 'bar-fill');
      var pct = total > 0 ? Math.max(0, Math.min(100, (done / total) * 100)) : 0;
      fill.style.width = pct + '%';
      bar.appendChild(fill);
      progress.appendChild(bar);
      card.appendChild(progress);

      var agentsBox = el('div', 'project-agents');
      var perProject = (p.agents && typeof p.agents === 'object') ? p.agents : {};
      var agentNames = KNOWN_AGENTS.slice();
      Object.keys(perProject).forEach(function (key) {
        if (agentNames.indexOf(key) === -1) agentNames.push(key);
      });
      agentNames.forEach(function (key) {
        agentsBox.appendChild(agentRow(key, perProject[key]));
      });
      card.appendChild(agentsBox);

      var gatesBox = el('div', 'project-gates');
      gatesBox.appendChild(el('h4', 'project-sub', 'gate history'));
      var gates = projectGateEvents(p, name, data && data.events);
      if (gates.length === 0) {
        gatesBox.appendChild(el('p', 'gate-no-reasons', 'no gate verdicts yet'));
      } else {
        gates.slice(0, MAX_PROJECT_GATES).forEach(function (evt) {
          gatesBox.appendChild(gateEntry(evt, name));
        });
        if (gates.length > MAX_PROJECT_GATES) {
          gatesBox.appendChild(el('p', 'gate-no-reasons',
            '+ ' + (gates.length - MAX_PROJECT_GATES) + ' older verdicts'));
        }
      }
      card.appendChild(gatesBox);

      box.appendChild(card);
    });
  }

  /* ---------- merged agents ---------- */

  function renderAgents(agents) {
    var grid = document.getElementById('agents');
    grid.replaceChildren();

    var names = Object.keys(agents || {});
    // With no fleet data at all, keep the classic coder/tester placeholders
    // so the page still shows the expected shape of the fleet.
    if (names.length === 0) names = KNOWN_AGENTS.slice();
    names.sort();

    names.forEach(function (name) {
      var info = (agents && agents[name]) || null;

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
    });
  }

  /* ---------- merged activity feed ---------- */

  function renderEvents(events) {
    var feed = document.getElementById('activity');
    feed.replaceChildren();

    var rows = Array.isArray(events) ? events.slice() : [];
    // Server already sends newest-first; a stable sort on ts keeps that
    // guarantee even if the payload ever arrives unordered.
    sortNewestFirst(rows);
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
      row.appendChild(el('span', 'evt-project', evt.project ? String(evt.project) : '—'));
      row.appendChild(el('span', 'evt-kind ' + (KIND_CLASS[kind] || 'k-plain'), kind));

      var fields = el('span', 'evt-fields');
      Object.keys(evt).forEach(function (key) {
        if (key === 'ts' || key === 'kind' || key === 'project') return;
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

  /* ---------- merged gate history ---------- */

  function renderGates(events) {
    var section = document.getElementById('gate-history');
    var list = document.getElementById('gate-list');
    var merged = document.getElementById('count-merged');
    var rejected = document.getElementById('count-rejected');
    if (!list && section) {
      list = el('div', 'gate-list');
      list.id = 'gate-list';
      section.appendChild(list);
    }
    if (!list || !merged || !rejected) return;

    var rows = (Array.isArray(events) ? events : []).filter(function (evt) {
      return evt && (evt.kind === 'gate_green_merged' || evt.kind === 'gate_red');
    });
    sortNewestFirst(rows);

    var greens = 0;
    var reds = 0;
    rows.forEach(function (evt) {
      if (evt.kind === 'gate_green_merged') greens += 1; else reds += 1;
    });
    // Counters first: even if an individual entry below trips on an
    // unexpected payload, the totals stay truthful.
    merged.textContent = greens + ' merged';
    rejected.textContent = reds + ' rejected';

    list.replaceChildren();

    rows.forEach(function (evt) {
      var repo = evt.project ? String(evt.project) : 'loomlet';
      var entry = gateEntry(evt, repo);
      if (evt.project) {
        var head = entry.querySelector('.gate-head');
        if (head) head.insertBefore(el('span', 'gate-project', String(evt.project)),
          head.firstChild ? head.firstChild.nextSibling : null);
      }
      list.appendChild(entry);
    });

    if (rows.length === 0) {
      list.appendChild(el('div', 'feed-empty', 'no gate verdicts yet'));
    }
  }

  /* ---------- sync / polling ---------- */

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

  function renderSafely(fn, arg) {
    // A malformed payload in one section must not blank the others.
    try { fn(arg); } catch (err) {
      if (window.console && console.error) console.error('loomlet render:', err);
    }
  }

  function render(data) {
    renderSafely(renderPipeline, data);
    renderSafely(renderProjects, data);
    renderSafely(renderAgents, data && data.agents);
    renderSafely(renderGates, data && data.events);
    renderSafely(renderEvents, data && data.events);
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
