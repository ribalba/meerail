/* meerail statistics: the "Statistics" modal — per-account mail analytics over
   a chosen window.

   Everything on screen comes from one /api/analytics/overview call. The server
   does all the aggregation (see app/routers/analytics.py); this file is only
   layout, scales and colour, and it deliberately holds no arithmetic that could
   disagree with the database — the one exception is picking a heatmap step from
   the max, which is presentation.

   Charts are hand-built SVG and CSS grids rather than a charting library: the
   app has no build step and loads no third-party script, and the two shapes
   needed here (a two-series line and a bucket grid) are not worth breaking that
   for.

   Colour lives in mail.css as --an-* custom properties so light and dark are
   declared side by side with the rest of the theme. The two series colours were
   checked for colour-blind separation against both surfaces before being
   picked; the panels that use them also carry direct labels, so hue is never
   the only thing telling the two apart. */

App.stats = (function () {
  const $ = (s) => document.querySelector(s);

  // Window presets. The labels are what the user reads; the keys are the
  // server's `range` values.
  const RANGES = [
    ["7d", "7 days"],
    ["30d", "30 days"],
    ["90d", "90 days"],
    ["1y", "Year"],
    ["all", "All time"],
  ];

  const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  // Postgres extract(dow) is 0=Sunday; the week reads better Monday-first.
  const DOW_FROM_PG = [6, 0, 1, 2, 3, 4, 5];

  const STATE_KEY = "meerail.stats.state";

  let data = null;
  let loading = false;
  let error = "";
  let state = { account_id: null, range: "30d" };

  // --- Formatting ---
  const num = (n) => (n == null ? "—" : Number(n).toLocaleString());

  function fmtDur(seconds) {
    if (seconds == null) return "—";
    const s = Math.round(seconds);
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.round(s / 60)}m`;
    if (s < 86400) {
      const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
      return m ? `${h}h ${m}m` : `${h}h`;
    }
    const d = Math.floor(s / 86400), h = Math.round((s % 86400) / 3600);
    return h ? `${d}d ${h}h` : `${d}d`;
  }

  const pct = (v) => (v == null ? "—" : `${(v * 100).toFixed(1)}%`);

  // Bucket boundaries are dates at midnight local; a week or month bucket wants
  // the date, a day bucket in a short window wants it too. Only the label
  // density changes, so one formatter covers all three grains.
  function bucketLabel(iso, grain) {
    const d = App.utcDate(iso);
    if (!d) return "";
    if (grain === "month") return d.toLocaleDateString([], { month: "short", year: "2-digit" });
    return d.toLocaleDateString([], { month: "short", day: "numeric" });
  }

  // --- Persistence ---
  // The account and window survive closing the modal: this is a panel people
  // reopen to check one number, and re-picking both every time is friction.
  function loadState() {
    try {
      const raw = JSON.parse(localStorage.getItem(STATE_KEY) || "{}");
      if (raw && typeof raw === "object") {
        if (RANGES.some(([k]) => k === raw.range)) state.range = raw.range;
        if (raw.account_id === null || Number.isInteger(raw.account_id)) {
          state.account_id = raw.account_id;
        }
      }
    } catch (_) { /* corrupt entry: fall back to the defaults above */ }
  }

  function saveState() {
    localStorage.setItem(STATE_KEY, JSON.stringify(state));
  }

  // --- Fetch ---
  async function refresh() {
    loading = true;
    error = "";
    render();
    try {
      data = await App.api.analytics({
        range: state.range,
        // Minutes east of UTC. getTimezoneOffset() counts the other way.
        tz_offset: -new Date().getTimezoneOffset(),
        ...(state.account_id == null ? {} : { account_id: state.account_id }),
      });
    } catch (e) {
      error = e.message || "Could not load statistics";
      data = null;
    }
    loading = false;
    render();
  }

  // --- Panels ---

  function kpis(d) {
    const t = d.totals, l = d.latency;
    // Six headline numbers, each a plain figure rather than a one-bar chart.
    // The reply and response tiles carry their basis underneath, because both
    // are meaningless without knowing how many messages they rest on.
    const tiles = [
      { label: "Received / day", value: num(t.received_per_day),
        sub: `${num(t.received)} in this window` },
      { label: "Sent / day", value: num(t.sent_per_day),
        sub: `${num(t.sent)} in this window` },
      { label: "Median reply", value: fmtDur(l.mine),
        sub: l.answered ? `over ${num(l.answered)} replies` : "no replies in window" },
      { label: "You reply to", value: pct(l.response_rate),
        sub: l.rate_basis ? `of ${num(l.rate_basis)} messages` : "nothing old enough yet" },
      { label: "They reply in", value: fmtDur(l.theirs),
        sub: l.answered_by_them ? `over ${num(l.answered_by_them)} replies` : "no replies in window" },
      { label: "Sent per received", value: t.sent_ratio == null ? "—" : t.sent_ratio.toFixed(2),
        sub: `${num(t.threads)} conversations` },
    ];
    return `<div class="an-kpis">${tiles.map((k) => `
      <div class="an-kpi">
        <div class="an-kpi-label">${App.esc(k.label)}</div>
        <div class="an-kpi-value">${App.esc(k.value)}</div>
        <div class="an-kpi-sub">${App.esc(k.sub)}</div>
      </div>`).join("")}</div>`;
  }

  /* Volume over time — the one real chart. Two series, so it gets a legend as
     well as the hover readout; identity is never carried by colour alone. */
  function volumeChart(d) {
    const rows = d.volume || [];
    if (rows.length < 2) {
      return panel("Volume over time", `<p class="an-empty">Not enough data in this window to draw a trend.</p>`);
    }
    const W = 720, H = 200, PAD = { l: 44, r: 12, t: 12, b: 24 };
    const iw = W - PAD.l - PAD.r, ih = H - PAD.t - PAD.b;
    const max = Math.max(1, ...rows.map((r) => Math.max(r.received, r.sent)));
    // Round the axis top to something a person would choose, so the gridline
    // labels are readable numbers rather than whatever the data happened to hit.
    const step = niceStep(max / 4);
    const top = Math.ceil(max / step) * step;

    const x = (i) => PAD.l + (rows.length === 1 ? iw / 2 : (i / (rows.length - 1)) * iw);
    const y = (v) => PAD.t + ih - (v / top) * ih;
    const path = (key) => rows.map((r, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(r[key]).toFixed(1)}`).join("");

    const grid = [];
    for (let v = 0; v <= top; v += step) {
      grid.push(`<line class="an-grid" x1="${PAD.l}" y1="${y(v)}" x2="${W - PAD.r}" y2="${y(v)}"/>`);
      grid.push(`<text class="an-axis" x="${PAD.l - 6}" y="${y(v) + 3.5}" text-anchor="end">${num(v)}</text>`);
    }
    // Roughly six date ticks whatever the bucket count, so the axis never turns
    // into overlapping text.
    const every = Math.max(1, Math.round(rows.length / 6));
    const ticks = rows.map((r, i) => (i % every === 0 || i === rows.length - 1)
      ? `<text class="an-axis" x="${x(i)}" y="${H - 6}" text-anchor="middle">${App.esc(bucketLabel(r.bucket, d.grain))}</text>`
      : "").join("");

    return panel("Volume over time", `
      <div class="an-legend">
        <span class="an-key"><i class="an-swatch recv"></i>Received</span>
        <span class="an-key"><i class="an-swatch sent"></i>Sent</span>
      </div>
      <div class="an-chart" id="an-volume">
        <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img"
             aria-label="Received and sent mail per ${App.esc(d.grain)}">
          ${grid.join("")}
          ${ticks}
          <path class="an-line recv" d="${path("received")}"/>
          <path class="an-line sent" d="${path("sent")}"/>
          <line class="an-cross" id="an-cross" x1="0" y1="${PAD.t}" x2="0" y2="${PAD.t + ih}" hidden/>
          <circle class="an-dot recv" id="an-dot-recv" r="3.5" hidden/>
          <circle class="an-dot sent" id="an-dot-sent" r="3.5" hidden/>
        </svg>
        <div class="an-tip" id="an-tip" hidden></div>
      </div>`);
  }

  // 1 / 2 / 5 x 10^n — the standard set of axis steps that read as round.
  function niceStep(raw) {
    const p = Math.pow(10, Math.floor(Math.log10(Math.max(raw, 1))));
    const n = raw / p;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
  }

  function wireVolumeHover(d) {
    const host = $("#an-volume");
    if (!host) return;
    const rows = d.volume || [];
    const W = 720, PAD = { l: 44, r: 12 };
    const iw = W - PAD.l - PAD.r;
    const cross = $("#an-cross"), tip = $("#an-tip");
    const dots = { received: $("#an-dot-recv"), sent: $("#an-dot-sent") };
    const svg = host.querySelector("svg");

    function hide() {
      cross.hidden = true; tip.hidden = true;
      dots.received.hidden = true; dots.sent.hidden = true;
    }

    host.addEventListener("mousemove", (e) => {
      const box = svg.getBoundingClientRect();
      // The SVG is stretched to the panel width, so map the pointer back into
      // viewBox units before looking up an index.
      const vx = ((e.clientX - box.left) / box.width) * W;
      const i = Math.max(0, Math.min(rows.length - 1,
        Math.round(((vx - PAD.l) / iw) * (rows.length - 1))));
      const r = rows[i];
      if (!r) return hide();
      const px = PAD.l + (rows.length === 1 ? iw / 2 : (i / (rows.length - 1)) * iw);
      cross.setAttribute("x1", px); cross.setAttribute("x2", px);
      cross.hidden = false;
      for (const key of ["received", "sent"]) {
        const el = dots[key];
        el.setAttribute("cx", px);
        el.setAttribute("cy", chartY(r[key], d));
        el.hidden = false;
      }
      tip.hidden = false;
      tip.innerHTML =
        `<div class="an-tip-head">${App.esc(bucketLabel(r.bucket, d.grain))}</div>` +
        `<div><i class="an-swatch recv"></i>Received <b>${num(r.received)}</b></div>` +
        `<div><i class="an-swatch sent"></i>Sent <b>${num(r.sent)}</b></div>`;
      // Keep the tooltip inside the panel rather than letting it run off the
      // right-hand edge on the last few buckets.
      const rel = (px / W) * box.width;
      const flip = rel > box.width - 130;
      tip.style.left = `${flip ? rel - 120 : rel + 12}px`;
    });
    host.addEventListener("mouseleave", hide);
  }

  // Kept in step with volumeChart's scale by recomputing it the same way.
  function chartY(v, d) {
    const rows = d.volume || [];
    const H = 200, PAD = { t: 12, b: 24 };
    const ih = H - PAD.t - PAD.b;
    const max = Math.max(1, ...rows.map((r) => Math.max(r.received, r.sent)));
    const step = niceStep(max / 4);
    const top = Math.ceil(max / step) * step;
    return PAD.t + ih - (v / top) * ih;
  }

  /* When mail arrives, by weekday and local hour. Magnitude, so it takes a
     single-hue sequential ramp rather than the two series colours. */
  function heatmap(d) {
    const cells = d.heatmap || [];
    if (!cells.length) return "";
    const grid = {};
    let max = 0;
    for (const c of cells) {
      const row = DOW_FROM_PG[c.dow];
      const key = `${row}:${c.hour}`;
      grid[key] = (grid[key] || 0) + c.received;
      max = Math.max(max, grid[key]);
    }
    const body = DOW.map((name, row) => {
      const tds = [];
      for (let h = 0; h < 24; h++) {
        const n = grid[`${row}:${h}`] || 0;
        // Square-root rather than linear: mail volume is heavily tailed (one
        // newsletter burst can be 50x a normal hour), and on a linear ramp that
        // single peak drags every other cell into the lightest step and the
        // panel reads as uniformly empty. Step 0 is reserved for "nothing at
        // all", so a zero cell never reads as a low-but-present count.
        const stepIdx = n === 0 ? 0 : Math.max(1, Math.ceil(Math.sqrt(n / max) * 6));
        tds.push(`<i class="an-cell s${stepIdx}" title="${App.esc(name)} ${h}:00 — ${num(n)} received"></i>`);
      }
      return `<div class="an-hm-row"><span class="an-hm-day">${App.esc(name)}</span>
        <div class="an-hm-cells">${tds.join("")}</div></div>`;
    }).join("");
    return panel("When mail arrives", `
      <div class="an-hm">${body}
        <div class="an-hm-row an-hm-axis"><span class="an-hm-day"></span>
          <div class="an-hm-cells">${
            [0, 6, 12, 18].map((h) => `<span style="grid-column:${h + 1}/span 6">${h}:00</span>`).join("")
          }</div></div>
      </div>
      <div class="an-hm-legend">
        <span>Less</span>${[0, 1, 2, 3, 4, 5, 6].map((i) => `<i class="an-cell s${i}"></i>`).join("")}<span>More</span>
        <span class="an-note">Peak ${num(max)} in one hour · your local time</span>
      </div>`);
  }

  /* Who you actually exchange mail with. Two bars per person on a shared scale,
     each carrying its own number, so the split reads without the legend. */
  function correspondents(d) {
    const rows = d.correspondents || [];
    if (!rows.length) return "";
    const max = Math.max(...rows.map((r) => Math.max(r.received, r.sent)), 1);
    const body = rows.map((r) => `
      <li class="an-corr">
        <div class="an-corr-who" title="${App.esc(r.address)}">
          <span class="an-corr-name">${App.esc(r.name || r.address)}</span>
          ${r.name && r.name !== r.address
            ? `<span class="an-corr-addr">${App.esc(r.address)}</span>` : ""}
        </div>
        <div class="an-corr-bars">
          <div class="an-pair">
            <div class="an-track"><i class="an-fill recv" style="width:${(r.received / max) * 100}%"></i></div>
            <span class="an-pair-n">${num(r.received)}</span>
          </div>
          <div class="an-pair">
            <div class="an-track"><i class="an-fill sent" style="width:${(r.sent / max) * 100}%"></i></div>
            <span class="an-pair-n">${num(r.sent)}</span>
          </div>
        </div>
      </li>`).join("");
    return panel("Top correspondents", `
      <div class="an-legend">
        <span class="an-key"><i class="an-swatch recv"></i>From them</span>
        <span class="an-key"><i class="an-swatch sent"></i>To them</span>
      </div>
      <ul class="an-corr-list">${body}</ul>`);
  }

  function latency(d) {
    const l = d.latency;
    const buckets = l.buckets || [];
    const total = buckets.reduce((a, b) => a + b.count, 0);
    if (!total) {
      return panel("How fast you reply", `<p class="an-empty">
        No replies from you inside this window, so there is nothing to measure.</p>`);
    }
    const max = Math.max(...buckets.map((b) => b.count), 1);
    const body = buckets.map((b) => `
      <li class="an-bar-row">
        <span class="an-bar-label">${App.esc(b.label)}</span>
        <div class="an-track"><i class="an-fill seq" style="width:${(b.count / max) * 100}%"></i></div>
        <span class="an-bar-n">${num(b.count)}</span>
      </li>`).join("");
    return panel("How fast you reply", `
      <ul class="an-bar-list">${body}</ul>
      <p class="an-note">Median ${App.esc(fmtDur(l.mine))} · 90th percentile
        ${App.esc(fmtDur(l.mine_p90))} · ${num(total)} replies</p>`);
  }

  function domains(d) {
    const rows = d.domains || [];
    if (!rows.length) return "";
    const max = Math.max(...rows.map((r) => r.count), 1);
    const body = rows.map((r) => `
      <li class="an-bar-row">
        <span class="an-bar-label" title="${App.esc(r.domain)}">${App.esc(r.domain)}</span>
        <div class="an-track"><i class="an-fill seq" style="width:${(r.count / max) * 100}%"></i></div>
        <span class="an-bar-n">${num(r.count)}</span>
      </li>`).join("");
    return panel("Where mail comes from", `<ul class="an-bar-list">${body}</ul>`);
  }

  function threadsPanel(d) {
    const t = d.threads || {};
    if (!t.longest || !t.longest.length) return "";
    const rows = t.longest.map((x) => `
      <tr><td class="an-thread-subj">${App.esc(x.subject)}</td>
          <td class="an-thread-n">${num(x.count)}</td></tr>`).join("");
    const a = d.attachments || {};
    return panel("Conversations", `
      <dl class="ag-stats">
        <div><dt>Average length</dt><dd>${App.esc((t.avg || 0).toFixed(2))} messages</dd></div>
        <div><dt>With a reply</dt><dd>${num(t.multi)}</dd></div>
        <div><dt>Attachments</dt><dd>${num(a.count)} · ${App.esc(App.fmtSize(a.bytes) || "0 B")}</dd></div>
        <div><dt>Busiest day</dt><dd>${d.busiest
          ? App.esc(`${App.fmtDate(d.busiest.day)} · ${num(d.busiest.received)}`) : "—"}</dd></div>
      </dl>
      <table class="an-threads">
        <thead><tr><th>Longest conversations</th><th class="an-thread-n">Messages</th></tr></thead>
        <tbody>${rows}</tbody></table>`);
  }

  function panel(title, inner) {
    return `<section class="an-panel"><h3>${App.esc(title)}</h3>${inner}</section>`;
  }

  /* The methodology note. This is not decoration: several numbers above rest on
     inferences the schema does not record, and a reader who does not know that
     will over-trust them. */
  function footnote(d) {
    const l = d.latency || {};
    return `<details class="an-about">
      <summary>How these numbers are worked out</summary>
      <ul>
        <li><b>Sent vs received</b> is inferred, not stored — a message counts as
          yours if it came from one of this account's addresses, or if it sits in
          the Sent folder.</li>
        <li><b>A reply</b> is the next message from the other side in the same
          conversation, counted only if it landed within
          ${App.esc(String(l.window_days ?? 30))} days. Conversation grouping comes
          from mail headers with a subject-matching fallback, so a thread that was
          never linked properly will not show a reply here even if you did answer.</li>
        <li><b>Response rate</b> only counts mail at least
          ${App.esc(String(l.maturity_days ?? 7))} days old, so a message you have
          not got to yet is not scored as one you ignored.</li>
        <li><b>Drafts and junk are excluded</b> throughout. Times are your local
          time; the mail store keeps UTC.</li>
      </ul>
    </details>`;
  }

  // --- Render ---
  function controls() {
    const accounts = (data && data.accounts) || [];
    const opts = [`<option value="">All accounts</option>`]
      .concat(accounts.map((a) =>
        `<option value="${a.id}"${state.account_id === a.id ? " selected" : ""}>${
          App.esc(a.label || a.email)}</option>`));
    return `<div class="an-controls">
      <select id="an-account" class="search-select" title="Account">${opts.join("")}</select>
      <div class="an-ranges" role="group" aria-label="Time range">
        ${RANGES.map(([k, label]) =>
          `<button type="button" class="an-range${state.range === k ? " on" : ""}"
                   data-range="${k}">${App.esc(label)}</button>`).join("")}
      </div>
    </div>`;
  }

  function render() {
    const body = $("#stats-body");
    if (!body) return;
    // The account list only arrives with the payload, so the picker is drawn
    // from the last good response and left in place across reloads — otherwise
    // it would disappear on every range change.
    const head = controls();
    if (error) {
      body.innerHTML = head + `<p class="an-error">${App.esc(error)}</p>`;
      wireControls();
      return;
    }
    if (loading && !data) {
      body.innerHTML = head + `<p class="muted small">Loading…</p>`;
      wireControls();
      return;
    }
    if (!data) return;
    if (!data.totals.messages) {
      body.innerHTML = head + `<p class="an-empty">No mail in this window.</p>`;
      wireControls();
      return;
    }
    body.innerHTML =
      head +
      (loading ? `<div class="an-reloading">Updating…</div>` : "") +
      kpis(data) +
      volumeChart(data) +
      heatmap(data) +
      correspondents(data) +
      `<div class="an-two">${latency(data)}${domains(data)}</div>` +
      threadsPanel(data) +
      footnote(data);
    wireControls();
    wireVolumeHover(data);
  }

  // Re-bound after every render, because the markup above is replaced wholesale.
  function wireControls() {
    const sel = $("#an-account");
    if (sel) {
      sel.addEventListener("change", () => {
        const v = sel.value;
        state.account_id = v === "" ? null : Number(v);
        saveState();
        refresh();
      });
    }
    document.querySelectorAll("#stats-body .an-range").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.range = btn.dataset.range;
        saveState();
        refresh();
      });
    });
  }

  // --- Modal ---
  function isOpen() { return !$("#stats-modal").hidden; }

  function open() {
    $("#stats-modal").hidden = false;
    render();
    refresh();
  }

  function close() { $("#stats-modal").hidden = true; }

  function init() {
    loadState();
    $("#btn-stats").innerHTML = App.icon("stats", 17);
    $("#btn-stats").addEventListener("click", open);
    $("#btn-close-stats").innerHTML = App.icon("close", 18);
    $("#btn-close-stats").addEventListener("click", close);
    $("#stats-modal").addEventListener("click", (e) => {
      if (e.target.id === "stats-modal") close();
    });
  }

  return { init, open, close, isOpen };
})();
