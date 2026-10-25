/* Veto Sentinel console.
 *
 * Polls the JSON API (status 1 s, history 2 s incremental, transitions 3 s) and renders it for
 * non-experts: plain-English explanations first, technical details behind "Expert details".
 * Untrusted text (headlines, reasons, names) is only ever inserted with textContent; innerHTML is
 * used for the static icon markup defined in this file.
 */
(() => {
  "use strict";

  // ================================================================ helpers
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
  const fmt = (x, d = 2) => (x == null || !Number.isFinite(x) ? "–" : Number(x).toFixed(d));
  const css = (name) => getComputedStyle(document.body).getPropertyValue(name).trim();

  function h(tag, props, ...children) {
    const el = document.createElement(tag);
    if (props) {
      for (const [k, v] of Object.entries(props)) {
        if (v == null || v === false) continue;
        if (k === "class") el.className = v;
        else if (k === "text") el.textContent = v;
        else if (k === "html") el.innerHTML = v; // trusted static markup only (icons)
        else if (k === "style") for (const [sk, sv] of Object.entries(v)) el.style.setProperty(sk, sv);
        else if (k === "dataset") Object.assign(el.dataset, v);
        else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
        else el.setAttribute(k, v === true ? "" : String(v));
      }
    }
    for (const c of children.flat()) {
      if (c == null || c === false) continue;
      el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }
  function setText(el, v) {
    const s = v == null ? "" : String(v);
    if (el && el.textContent !== s) el.textContent = s;
  }
  function withAlpha(color, a) {
    const m = /^#([0-9a-f]{6})$/i.exec(color.trim());
    if (!m) return color;
    const n = parseInt(m[1], 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  }
  function tween(el, to, decimals) {
    const from = parseFloat(el.dataset.v);
    el.dataset.v = String(to);
    cancelAnimationFrame(el._raf);
    if (!Number.isFinite(from) || reduceMotion) {
      setText(el, to.toFixed(decimals));
      return;
    }
    const t0 = performance.now();
    const step = (t) => {
      const k = Math.min(1, (t - t0) / 900);
      const e = 1 - Math.pow(1 - k, 3);
      setText(el, (from + (to - from) * e).toFixed(decimals));
      if (k < 1) el._raf = requestAnimationFrame(step);
    };
    el._raf = requestAnimationFrame(step);
  }
  function safeJSON(s) {
    try { return typeof s === "string" ? JSON.parse(s) : s || {}; } catch (_) { return {}; }
  }
  function joinAnd(parts) {
    if (parts.length <= 1) return parts.join("");
    return parts.slice(0, -1).join(", ") + " and " + parts[parts.length - 1];
  }

  // ================================================================ time formatting (UTC)
  const DTF = new Intl.DateTimeFormat("en-GB", {
    timeZone: "UTC", weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  });
  function parts(iso) {
    const p = {};
    for (const x of DTF.formatToParts(new Date(iso))) p[x.type] = x.value;
    return p;
  }
  const fmtFull = (iso) => { if (!iso) return "–"; const p = parts(iso); return `${p.weekday} ${p.day} ${p.month}, ${p.hour}:${p.minute}`; };
  const fmtClock = (iso) => { const p = parts(iso); return `${p.hour}:${p.minute}`; };
  const fmtDay = (iso) => { if (!iso) return "–"; const p = parts(iso); return `${p.weekday} ${p.day} ${p.month}`; };
  function fmtDuration(min) {
    if (min < 60) return `${Math.max(1, Math.round(min))} min`;
    const hrs = Math.floor(min / 60), m = Math.round(min % 60);
    if (hrs < 48) return m ? `${hrs} h ${m} min` : `${hrs} h`;
    return `${Math.round(hrs / 24)} days`;
  }

  // ================================================================ icons
  const P = {
    check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    x: '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
    alert: '<path d="M12 3.6 2.6 19.8h18.8z"/><path d="M12 10v4.4"/><path d="M12 17.4v.2"/>',
    stop: '<path d="M8.2 3h7.6L21 8.2v7.6L15.8 21H8.2L3 15.8V8.2z"/><path d="M8.5 12h7"/>',
    siren: '<path d="M7 18v-6a5 5 0 0 1 10 0v6"/><path d="M5 21h14"/><path d="M12 3v1.5M4.6 6.2l1.2 1.2M19.4 6.2l-1.2 1.2"/>',
    grid: '<rect x="3.5" y="3.5" width="7" height="7" rx="1.8"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.8"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.8"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.8"/>',
    signals: '<path d="M4 20v-6M9 20V9M14 20v-8M19 20V4"/>',
    timeline: '<path d="M3 17l5-6 4 3 5-7 4 4"/><path d="M3 21h18"/>',
    news: '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="M7 9h10M7 12.5h10M7 16h6"/>',
    bot: '<rect x="5" y="8.5" width="14" height="11" rx="3"/><path d="M12 4.5v4M9 13.5v.6M15 13.5v.6M9.5 17h5"/>',
    sliders: '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.6v.2"/>',
    arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
    play: '<path d="M8 5.5v13l11-6.5z" fill="currentColor"/>',
    pause: '<path d="M8.5 5.5v13M15.5 5.5v13" stroke-width="3"/>',
    step: '<path d="M6 6v12l8.5-6z"/><path d="M18 6v12"/>',
    restart: '<path d="M4.5 12a7.5 7.5 0 1 0 2.3-5.4"/><path d="M4 4.5v4h4"/>',
    forward: '<path d="M4 6v12l7-6zM12 6v12l7-6z"/>',
    scissors: '<circle cx="6" cy="6" r="2.6"/><circle cx="6" cy="18" r="2.6"/><path d="M8.2 7.6 20 19M8.2 16.4 20 5"/>',
    mailx: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7.5l9 6 9-6"/><path d="M15 15l3.5 3.5M18.5 15 15 18.5"/>',
    gap: '<path d="M3 12h4M10 12h4M17 12h4"/><path d="M12 5v2M12 17v2"/>',
    broadcast: '<circle cx="12" cy="12" r="2"/><path d="M8.5 8.5a5 5 0 0 0 0 7M15.5 8.5a5 5 0 0 1 0 7M5.6 5.6a9 9 0 0 0 0 12.8M18.4 5.6a9 9 0 0 1 0 12.8"/>',
    bolt: '<path d="M13 2.5 4.5 14H11l-1 7.5L18.5 10H12z"/>',
    shield: '<path d="M12 3l7.5 2.8v5.7c0 4.6-3.2 8.2-7.5 9.5-4.3-1.3-7.5-4.9-7.5-9.5V5.8z"/><path d="M9 12l2.2 2.2L15.5 10"/>',
    face: '<circle cx="12" cy="12" r="9"/><path d="M8.5 16.4a5 5 0 0 1 7 0M9 10h.01M15 10h.01"/>',
    shuffle: '<path d="M4 7h4l8 10h4M4 17h4l2.5-3M14 10l2-3h4"/><path d="M18 4.5 20.5 7 18 9.5M18 14.5l2.5 2.5-2.5 2.5"/>',
    bars: '<path d="M4 20v-4M8 20v-9M12 20v-6M16 20V5M20 20v-8"/>',
    filter: '<path d="M4 5h16l-6 7.5V19l-4 1.5v-8z"/>',
    gauge: '<path d="M4.5 16a7.5 7.5 0 1 1 15 0"/><path d="M12 16l3.5-4.5"/>',
    scale: '<path d="M12 4v16M6 20h12M5 8h14"/><path d="M5 8l-2.5 6a3 3 0 0 0 5 0zM19 8l-2.5 6a3 3 0 0 0 5 0z"/>',
    plug: '<path d="M9 3v5M15 3v5M6 8h12v3a6 6 0 0 1-12 0zM12 17v4"/>',
    bank: '<path d="M3 9.5 12 4l9 5.5M5 10v8M9.5 10v8M14.5 10v8M19 10v8M3 20.5h18"/>',
    flask: '<path d="M9 3h6M10 3v6L4.5 18.5A1.8 1.8 0 0 0 6 21h12a1.8 1.8 0 0 0 1.5-2.5L14 9V3"/><path d="M7.5 15h9"/>',
    cpu: '<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2.5v3.5M15 2.5v3.5M9 18v3.5M15 18v3.5M2.5 9h3.5M2.5 15h3.5M18 9h3.5M18 15h3.5"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6l1.4 1.4M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4 6 18M18 6l1.4-1.4"/>',
    moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
    chevron: '<path d="M6 9l6 6 6-6"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    target: '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.5"/><circle cx="12" cy="12" r="1" fill="currentColor"/>',
    oil: '<path d="M12 3.5s6 6.6 6 10.6a6 6 0 0 1-12 0c0-4 6-10.6 6-10.6z"/>',
    coin: '<circle cx="12" cy="12" r="8.5"/><path d="M14.5 9.2c-.6-.8-1.5-1.2-2.5-1.2-1.6 0-2.7.9-2.7 2s1 1.7 2.7 2 2.7.9 2.7 2-1.1 2-2.7 2c-1 0-1.9-.4-2.5-1.2M12 6.5V8M12 16v1.5"/>',
    globe: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.4 2.4 3.5 5.2 3.5 8.5s-1.1 6.1-3.5 8.5c-2.4-2.4-3.5-5.2-3.5-8.5s1.1-6.1 3.5-8.5z"/>',
    heart: '<path d="M12 20s-7.5-4.6-7.5-10.2A4.2 4.2 0 0 1 12 7.3a4.2 4.2 0 0 1 7.5 2.5C19.5 15.4 12 20 12 20z"/>',
  };
  const icon = (name, cls = "") => `<svg class="ico ${cls}" viewBox="0 0 24 24" aria-hidden="true">${P[name] || ""}</svg>`;
  function hydrateIcons(root = document) {
    for (const el of $$("[data-icon]", root)) if (!el.firstElementChild) el.innerHTML = icon(el.dataset.icon);
  }

  // ================================================================ plain-English copy
  const LEVELS = {
    NORMAL: { title: "All clear", code: "NORMAL", icon: "check", tone: "good",
      desc: "The news looks normal. Trading bots can trade as usual." },
    CAUTION: { title: "Be careful", code: "CAUTION", icon: "alert", tone: "warning",
      desc: "Something unusual is going on in the news. Bots stop opening new positions and keep their size small until things calm down." },
    HALT: { title: "Stop", code: "HALT", icon: "stop", tone: "critical",
      desc: "Either the news shows serious stress, or Veto Sentinel can't see clearly enough to give the all-clear. Bots must not place new orders." },
    FLATTEN: { title: "Emergency", code: "FLATTEN", icon: "siren", tone: "critical",
      desc: "A human operator has issued an emergency instruction to close positions. Veto Sentinel never trades by itself — a person has to act on it." },
  };
  const level = (s) => LEVELS[s] || LEVELS.HALT;

  const SIGNALS = [
    { key: "panic", z: "panic_z", name: "Negativity", q: "How negative does the news sound?", icon: "face", color: "--s1",
      rise: "the news turned much more negative",
      why: ["The news sounds a bit more negative than usual.", "The news sounds clearly more negative than usual.", "The news sounds far more negative than usual."],
      explain: "An AI trained on financial text (FinBERT) reads every headline and scores how negative it sounds. Veto Sentinel averages the last hour — trusted sources count more — and compares it with the past week.",
      fallback: "negativity is scored with a simple word list instead of the FinBERT AI" },
    { key: "drift", z: "drift_z", name: "Topic change", q: "Is the news suddenly about something new?", icon: "shuffle", color: "--s2",
      rise: "the news switched to unusual topics",
      why: ["The news is drifting a little away from its usual topics.", "The news is focusing on clearly different topics than usual.", "The news has switched to completely different topics."],
      explain: "A language AI (MiniLM) turns each headline into a vector: a list of 384 numbers that captures what it is about, so headlines on similar topics get similar vectors. Veto Sentinel compares the last hour's topics with the past week's. A big gap means the story has changed — like a sudden wave of bank-run headlines.",
      fallback: "topic change uses simple word matching instead of the MiniLM AI" },
    { key: "volume", z: "volume_z", name: "News volume", q: "Is there an unusual amount of news?", icon: "bars", color: "--s3",
      rise: "the amount of news jumped",
      why: ["There's a bit more news than usual for this time of day.", "There's clearly more news than usual for this time of day.", "There's a flood of news compared with a normal day."],
      explain: "Veto Sentinel counts unique headlines in the last hour and compares that with the same hour on previous days (weekdays and weekends separately). Crises usually come with a burst of news.",
      fallback: "" },
  ];

  const HELP = {
    risk: "The risk score combines the three warning signals into one number. 0 means a typical day. Above 2 Veto Sentinel says “be careful”; above 3.5 it says “stop”. It only relaxes after things have stayed calm for a while, so it doesn't flip-flop.",
    bots: "Trading bots connected to Veto Sentinel check this before every order. Veto Sentinel never places or cancels trades itself — it only tells bots how careful to be.",
    weights: "Each signal is first compared with its own normal range (that's the number on each card). Then they're blended: 40% negativity, 30% topic change and 30% news volume. Readings at or below normal count as zero, so a calm signal can't hide a stressed one.",
    headlines: "These are the headlines from the last hour that the AI scored as most negative. Copies of the same story from different outlets are counted once.",
    flatten: "FLATTEN is the only level a human sets. It's for situations where people decide positions should be closed. Veto Sentinel records who asked and why and broadcasts it. Closing positions is always done by a person, never automatically.",
  };

  const FAULTS = {
    startup: ["Start-up wait", "It had only just started and was waiting for its first good heartbeats."],
    stale_heartbeat: ["Heartbeat stopped", "It stopped hearing Veto Sentinel's heartbeat, so it blocks all orders."],
    invalid_message: ["Corrupted message", "It received a garbled message and waits for two good ones."],
    sequence_gap: ["Missing messages", "Some messages went missing; it waits for two good ones in a row."],
    duplicate_seq: ["Repeated message", "It received the same message twice."],
    out_of_order_seq: ["Out-of-order message", "It received an older message after a newer one."],
    publisher_restart: ["Veto Sentinel restarted", "Veto Sentinel restarted, so the bot re-syncs from scratch."],
    stale_timestamp: ["Out-of-date message", "A message arrived too late to trust."],
    future_timestamp: ["Clock problem", "A message's timestamp was in the future."],
    monitor_not_ready: ["Monitor not ready", "Veto Sentinel says it can't see clearly yet (warming up or missing data)."],
  };

  const SCENARIOS = {
    feed_outage: { title: "News feed outage", text: "Three hours with no news at all. Watch Veto Sentinel refuse to treat silence as calm.", icon: "plug" },
    policy_surprise: { title: "Surprise rate hike", text: "A fictional central bank shocks markets with a large, unexpected rate rise.", icon: "bank" },
    bank_stress: { title: "Bank run", text: "A fictional regional bank collapses and fear spreads through the news.", icon: "alert" },
    svb_failure_window: { title: "SVB failure (March 2023)", text: "Approximate window around the Silicon Valley Bank failure, using real headlines from GDELT.", icon: "bank" },
  };
  function scenarioCopy(sc) {
    const c = SCENARIOS[sc.name];
    if (c) return c;
    const t = String(sc.name || "event").replace(/_/g, " ");
    return { title: t.charAt(0).toUpperCase() + t.slice(1), text: sc.kind === "data_gap" ? "A gap in the news data." : "A labelled event in this replay.", icon: "alert" };
  }

  const PIPE = [
    { icon: "news", t: "Read", s: "headlines", title: "1 · Read the news",
      text: "Veto Sentinel collects headlines from central banks (the Fed, ECB and Bank of England), a global news index (GDELT) and, in this demo, a replay of invented news. It never looks at prices." },
    { icon: "filter", t: "Clean", s: "de-duplicate", title: "2 · Clean it up",
      text: "Every headline gets a source and a timestamp. Copies of the same story from different outlets are counted once, and broken records are thrown away." },
    { icon: "signals", t: "Measure", s: "3 signals", title: "3 · Measure three warning signals",
      text: "How negative the news sounds, whether it's suddenly about new topics, and whether there's unusually much of it — each compared with its own normal." },
    { icon: "gauge", t: "Score", s: "one number", title: "4 · Combine them into a risk score",
      text: "40% negativity, 30% topic change and 30% news volume. 0 means a normal day; the higher, the more unusual. A detector also watches for sudden, sustained jumps." },
    { icon: "scale", t: "Decide", s: "a level", title: "5 · Decide a level",
      text: "All clear, be careful or stop. It reacts to danger immediately but waits for 30 calm minutes before relaxing one step, so it doesn't flip-flop. Missing or unclear data never counts as calm." },
    { icon: "broadcast", t: "Broadcast", s: "every second", title: "6 · Send a heartbeat",
      text: "Every second it sends the current level to every connected trading bot. Messages are numbered, so a missing one is noticed straight away." },
    { icon: "shield", t: "Protect", s: "bots obey", title: "7 · Bots protect themselves",
      text: "Each bot checks its own copy of the level before every order, in nanoseconds. If the heartbeat stops or looks wrong, the bot stops trading on its own." },
  ];

  const GLOSSARY = [
    ["Risk score", "One number that sums up how unusual the news is right now. 0 is a normal day; above 2 is unusual; above 3.5 is very unusual."],
    ["“Normal”", "For each signal Veto Sentinel remembers the past week (for news volume, the same time on previous days). “Unusual” always means unusual compared with that."],
    ["Heartbeat", "A small message Veto Sentinel sends every second. Bots treat silence as danger: no heartbeat for 3 seconds and they stop."],
    ["Warming up", "When Veto Sentinel starts, it needs a few days of news before it knows what normal looks like. Until then it keeps bots stopped."],
    ["Missing data", "If a signal can't be measured (for example too few headlines in the last hour), Veto Sentinel won't give the all-clear — at best it says “be careful”."],
    ["Sudden-change detector", "A statistical alarm (CUSUM) that fires when the risk score stays raised for a while, even if it never jumps past a threshold."],
    ["Simulated time", "In a replay, days of news are played back in minutes. Times shown are the replay's clock, not today's."],
    ["Demo data", "The default replay uses invented news about fictional banks and countries, so you can watch the system react safely."],
    ["Optimize for", "Tells Veto Sentinel what you trade (for example “OPEC, crude oil”). Each headline gets a relevance score from its AI vector; matching news counts fully, other news a little. Every “normal” is recomputed with the same weights, so the risk score still compares like with like."],
  ];

  // ================================================================ app state
  const S = {
    status: null, runId: null, history: [], transitions: [], transKey: "", prevState: null, heroState: null,
    tab: "overview", range: 0, series: { panic_z: false, drift_z: false, volume_z: false }, table: false,
    offline: false, user: null, lastValid: null, lastStateToast: 0, pipeStep: 0, codeExpires: null,
    eventsKey: "", markersKey: "", headKey: "", whyKey: "", faultCounts: {}, tilesKey: "",
  };

  // ================================================================ API
  async function api(path, opts = {}) {
    const r = await fetch(path, { credentials: "same-origin", cache: "no-store", ...opts });
    let body = null;
    try { body = await r.json(); } catch (_) { /* empty body */ }
    if (r.status === 401 && body && body.detail === "login required") {
      location.assign("/login");
      throw new Error("login required");
    }
    if (!r.ok) throw new Error((body && body.detail) || r.statusText || `HTTP ${r.status}`);
    return body;
  }
  const post = (path, data) => api(path, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: data ? JSON.stringify(data) : undefined,
  });

  // ================================================================ toasts + popover
  const TONE_ICON = { good: "check", warning: "alert", critical: "stop", info: "info" };
  function toast(title, text, tone = "info", iconName) {
    const box = $("#toasts");
    const el = h("div", { class: "toast", dataset: { tone } },
      h("span", { class: "t-ico", html: icon(iconName || TONE_ICON[tone] || "info") }),
      h("b", { text: title }), h("p", { text }));
    box.append(el);
    while (box.children.length > 4) box.firstElementChild.remove();
    window.setTimeout(() => { el.classList.add("out"); window.setTimeout(() => el.remove(), 380); }, 5600);
  }

  const popover = $("#popover");
  let popFor = null;
  function closePopover() {
    if (popFor) popFor.setAttribute("aria-expanded", "false");
    popFor = null;
    popover.hidden = true;
  }
  function openPopover(btn) {
    if (popFor === btn) { closePopover(); return; }
    closePopover();
    setText($("#popoverText"), HELP[btn.dataset.help] || "");
    popover.hidden = false;
    const r = btn.getBoundingClientRect();
    const pw = popover.offsetWidth, ph = popover.offsetHeight;
    let left = r.right - pw;
    if (left < 12) left = 12;
    let top = r.bottom + 8;
    if (top + ph > window.innerHeight - 12) top = r.top - ph - 8;
    popover.style.left = `${left}px`;
    popover.style.top = `${top}px`;
    btn.setAttribute("aria-expanded", "true");
    popFor = btn;
  }

  // ================================================================ tabs
  const TABS = ["overview", "signals", "timeline", "news", "bots", "controls", "how"];
  const indicator = $(".seg-indicator");
  function placeIndicator() {
    const btn = $(`#tabbtn-${S.tab}`);
    if (!btn) return;
    indicator.style.width = `${btn.offsetWidth}px`;
    indicator.style.transform = `translateX(${btn.offsetLeft}px)`;
  }
  function selectTab(name, { focus = false } = {}) {
    if (!TABS.includes(name)) name = "overview";
    const prev = S.tab;
    S.tab = name;
    for (const t of TABS) {
      const btn = $(`#tabbtn-${t}`), panel = $(`#tab-${t}`);
      const on = t === name;
      btn.setAttribute("aria-selected", String(on));
      btn.tabIndex = on ? 0 : -1;
      if (on) {
        panel.hidden = false;
        if (prev !== name || !panel.classList.contains("is-active")) {
          panel.classList.remove("is-active");
          void panel.offsetWidth; // replay the entrance animation
          panel.classList.add("is-active");
        }
      } else {
        panel.hidden = true;
        panel.classList.remove("is-active");
      }
    }
    if (focus) $(`#tabbtn-${name}`).focus();
    if (prev !== name && window.scrollY > 0) window.scrollTo({ top: 0 }); // a new section starts at its top
    hideTips();
    placeIndicator();
    $(`#tabbtn-${name}`).scrollIntoView({ block: "nearest", inline: "nearest", behavior: reduceMotion ? "auto" : "smooth" });
    history.replaceState(null, "", `#${name}`);
    if (name === "bots") ecg.start(); else ecg.stop();
    renderCharts(true);
  }

  // ================================================================ reasons in plain English
  function plainNotReady(reason) {
    let r = String(reason || "");
    let m = /required component \w+ unavailable \((.*)\)$/.exec(r);
    if (m) r = m[1];
    if ((m = /time-of-day baseline warm-up: (\d+)\/(\d+) matching (weekday|weekend) days/.exec(r))) {
      return `It's still learning what a normal ${m[3]} looks like (${m[1]} of ${m[2]} ${m[3]}s of history so far).`;
    }
    if ((m = /baseline warm-up: (\d+)\/(\d+) samples/.exec(r))) return `It's still building its picture of normal news (${m[1]} of ${m[2]} readings so far).`;
    if (/current window not fully covered/.test(r)) return "Part of the last hour's news is missing (a gap in the data).";
    if (/ingestion stale/.test(r)) return "No fresh news has arrived for a while.";
    if ((m = /news data is (\d+) min behind/.exec(r))) return `The newest complete news is ${m[1]} minutes old, so it can't judge the present.`;
    if (/no successful news fetch yet/.test(r)) return "It hasn't received any news yet.";
    if (/pipeline stalled/.test(r)) return "Its analysis engine stopped responding.";
    if (/no news adapters/.test(r)) return "No news sources are connected.";
    if (/no risk component available/.test(r)) return "None of the three signals can be measured right now.";
    if (/no assessment yet/.test(r)) return "It hasn't finished its first check yet.";
    return r;
  }
  function plainDegraded(reason) {
    const sig = SIGNALS.find((x) => String(reason).startsWith(x.key + " "));
    if (!sig) return String(reason);
    if (/only \d+ headlines|insufficient samples/.test(reason)) return `Too few headlines in the last hour to measure ${sig.name.toLowerCase()}.`;
    if (/warm-up/.test(reason)) return `${sig.name} is still learning what normal looks like.`;
    if (/degenerate/.test(reason)) return `${sig.name} can't be measured reliably right now.`;
    return `${sig.name} can't be measured right now.`;
  }
  function zBand(z) { return z == null ? -1 : z >= 3.5 ? 2 : z >= 2 ? 1 : z >= 1 ? 0 : -1; }

  function reasons(s) {
    const out = [];
    if (!s.monitor_ready) {
      const seen = new Set();
      for (const r of s.not_ready_reasons || []) {
        const t = plainNotReady(r);
        if (!seen.has(t)) { seen.add(t); out.push({ text: t, icon: "info", cls: "info" }); }
      }
      out.push({ text: "Until it can see clearly it keeps bots stopped — better safe than sorry.", icon: "shield" });
      return out;
    }
    if (s.state === "FLATTEN" && s.flatten) {
      out.push({ text: `Set by ${s.flatten.operator}: ${s.flatten.reason}`, icon: "siren" });
      out.push({ text: "To lift it: Controls tab → “Cancel emergency”. It then resumes at Stop and relaxes step by step.", icon: "info", cls: "info" });
      return out;
    }
    for (const sig of SIGNALS) {
      const c = s.components[sig.key];
      const b = c && c.available ? zBand(c.z) : -1;
      if (b >= 0) out.push({ text: sig.why[b], icon: "arrow" });
    }
    const cp = s.decision && s.decision.changepoint;
    if (cp && cp.alarm_active) out.push({ text: "A sudden, sustained change in the news was detected in the last hour.", icon: "bolt" });
    if (s.degraded && (s.degraded_reasons || []).length) {
      for (const r of s.degraded_reasons) out.push({ text: plainDegraded(r), icon: "info", cls: "info" });
      out.push({ text: "Because some information is missing, Veto Sentinel won't give the all-clear.", icon: "shield" });
    }
    const reason = (s.decision && s.decision.reason) || "";
    const m = /cooling off, (\d+):(\d\d):\d\d remaining before (\w+)/.exec(reason)
      || /waiting for newer news before (\w+)/.exec(reason);
    if (m) {
      out.push({ text: `Things have calmed down. To avoid flip-flopping it waits before relaxing to “${level(m[3] || m[1]).title}” (see the countdown).`, icon: "info", cls: "info" });
    } else if (s.decision && s.decision.rule === "hold" && /exit condition/.test(reason)) {
      out.push({ text: "The risk score has eased, but not yet enough to relax the level.", icon: "info", cls: "info" });
    }
    if (!out.length) out.push({ text: "Negativity, topics and news volume are all in their normal range.", icon: "check" });
    const f = s.focus;
    if (f && f.text && f.effect && f.effect.focus_multiplier) {
      out.push({ text: `Optimized for “${f.text}”: news about it moves the score ×${f.effect.focus_multiplier.toFixed(1)} as much as before, other news ×${f.effect.other_multiplier.toFixed(2)}.`, icon: "target", cls: "info" });
    }
    return out;
  }

  function storyText(t) {
    const comps = safeJSON(t.components);
    const reason = String(t.reason || "");
    switch (t.rule) {
      case "escalate": {
        const ps = [];
        for (const sig of SIGNALS) if (comps[sig.key] != null && comps[sig.key] >= 2) ps.push(sig.rise);
        if (/change-point/.test(reason)) ps.push("a sudden, sustained shift was detected");
        if (/degraded data/.test(reason)) ps.push("some information went missing");
        return ps.length ? `Raised because ${joinAnd(ps)}.` : "Raised because the risk score went up.";
      }
      case "deescalate_after_cooldown": {
        const cool = S.status ? S.status.thresholds.cooldown_minutes : 30;
        return `Lowered one step after the news stayed calm for ${cool} minutes.`;
      }
      case "monitor_not_ready":
        return "Stopped as a precaution: " + plainNotReady(reason.replace(/^Monitor not ready:\s*/, "").split("; ")[0]);
      case "operator_flatten": return "A human operator issued an emergency instruction.";
      case "operator_clear_flatten": return "The operator cancelled the emergency instruction.";
      default: return reason;
    }
  }

  function zTone(z) {
    if (z == null) return { tone: "na", word: "No data" };
    if (z >= 3.5) return { tone: "critical", word: "Extreme" };
    if (z >= 2) return { tone: "serious", word: "High" };
    if (z >= 1) return { tone: "warning", word: "Slightly up" };
    return { tone: "good", word: "Normal" };
  }
  function permsFor(state, frac) {
    if (state === "NORMAL") return { open: true, reduce: true, size: 1 };
    if (state === "CAUTION") return { open: false, reduce: true, size: frac };
    return { open: false, reduce: false, size: 0 };
  }

  // ================================================================ top bar + notices
  function renderTop(s) {
    const L = level(s.state);
    setText($("#topStateWord"), L.title);
    $("#topStatus").setAttribute("aria-label", `Current level: ${L.title}`);
    setText($("#topRisk"), s.risk_score == null ? "–" : fmt(s.risk_score, 2));
    setText($("#topTime"), s.as_of ? `${fmtFull(s.as_of)}${s.mode === "replay" ? " · simulated" : " UTC"}` : "–");
  }
  function renderNotices(s) {
    $("#noticeSynthetic").hidden = !s.synthetic;
    const fb = SIGNALS.filter((x) => x.fallback && s.implementations[x.key] && s.implementations[x.key].is_fallback);
    $("#noticeFallback").hidden = !fb.length;
    if (fb.length) setText($("#fallbackText"), `The AI models weren't available, so ${joinAnd(fb.map((x) => x.fallback))}. Results are rougher than usual.`);
    $("#noticeOffline").hidden = !S.offline;
  }

  // ================================================================ overview
  function renderHero(s) {
    const L = level(s.state);
    const title = $("#heroTitle");
    if (S.heroState !== s.state) {
      setText(title, L.title);
      title.classList.remove("swap");
      void title.offsetWidth;
      title.classList.add("swap");
      const core = $("#orbIcon");
      core.innerHTML = icon(L.icon);
      core.classList.remove("pop");
      void core.offsetWidth;
      core.classList.add("pop");
      S.heroState = s.state;
    }
    setText($("#heroDesc"), L.desc);
    renderCalm(s);
    setText($("#heroRule"), s.decision ? `· rule ${s.decision.rule}` : "");
    const rs = reasons(s);
    const key = rs.map((r) => r.text).join("|");
    if (key !== S.whyKey) {
      S.whyKey = key;
      const ul = $("#heroWhy");
      ul.replaceChildren(...rs.map((r, i) => h("li", { class: r.cls || "", style: { "--i": String(i) } },
        h("span", { html: icon(r.icon || "arrow") }), h("span", { text: r.text }))));
    }
  }

  function mmss(sec) {
    const s = Math.max(0, Math.round(sec));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }
  // Countdown of the cooling-off period before the level relaxes one step. The server keeps the
  // clock (real time live, simulated time in a replay); between status polls the browser ticks it.
  let calmClock = null;
  function calmLeft() {
    return calmClock.remaining - calmClock.rate * ((performance.now() - calmClock.at) / 1000);
  }
  function tickCalm() {
    if (!calmClock) return;
    const left = calmLeft();
    if (!Number.isFinite(left)) { setText($("#calmTime"), "–"); return; }
    const words = left <= 0 && calmClock.waiting;
    $("#calmTime").classList.toggle("words", words);
    setText($("#calmTime"), words ? "waiting for news" : mmss(left));
    $("#calmBar").style.width = `${clamp(1 - Math.max(0, left) / calmClock.total, 0, 1) * 100}%`;
  }
  window.setInterval(tickCalm, 250);
  function renderCalm(s) {
    const box = $("#calm"), cd = s.cooldown, th = s.thresholds;
    const waitingFor = s.state === "HALT" || s.state === "CAUTION";
    if (!cd && !(waitingFor && s.monitor_ready)) { box.hidden = true; return; }
    box.hidden = false;
    box.classList.toggle("waiting", !cd);
    if (cd) {
      const r = s.replay;
      // The server owns the countdown (remaining_now_s); the browser only ticks between polls and
      // never lets the display jump back up within the same calm period.
      // (Falls back to the older fields if the server is an older version than this page.)
      const rate = Number.isFinite(cd.rate) ? cd.rate : (r.mode === "replay" ? (r.playing && !r.finished ? r.speed : 0) : 1);
      let remaining = Number.isFinite(cd.remaining_now_s) ? cd.remaining_now_s : cd.remaining_s;
      if (calmClock && calmClock.started === cd.started && rate > 0) remaining = Math.min(remaining, calmLeft());
      const waiting = !!cd.needs_newer_news && remaining <= 0;
      calmClock = { remaining, total: cd.total_s, rate, started: cd.started, at: performance.now(), waiting };
      setText($("#calmTitle"), `Relaxing to “${level(cd.next_state).title}” in`);
      tickCalm();
      if (waiting) {
        setText($("#calmNote"), `The news has stayed calm for ${Math.round(cd.total_s / 60)} minutes. Before relaxing, Veto Sentinel `
          + "waits for at least one newer batch of news to check, so a quiet moment is never just missing news. The news source is running late.");
        return;
      }
      const pace = r.mode === "replay" && r.playing && !r.finished && r.speed
        ? ` At the current replay speed that's about ${Math.max(1, Math.ceil(remaining / r.speed))} real second${Math.ceil(remaining / r.speed) === 1 ? "" : "s"}.`
        : r.mode === "replay" ? " The replay is paused, so the countdown is too." : "";
      setText($("#calmNote"), `The news must stay calm for ${Math.round(cd.total_s / 60)} minutes.${pace} If it flares up again, the countdown starts over.`);
      return;
    }
    calmClock = null;
    const exit = s.state === "HALT" ? th.halt_exit : th.caution_exit;
    const cp = s.decision && s.decision.changepoint && s.decision.changepoint.alarm_active;
    setText($("#calmTitle"), "Calm countdown not started yet");
    setText($("#calmTime"), "");
    let why = `It starts once the risk score falls below ${exit}`
      + (s.risk_score == null ? "." : ` (now ${fmt(s.risk_score, 2)}).`);
    if (s.state === "CAUTION" && s.degraded) why = "It starts once the missing information is back.";
    else if (cp) why += " The sudden-change alarm also has to clear first.";
    setText($("#calmNote"), `${why} Then the news has to stay calm for ${th.cooldown_minutes} minutes.`);
  }

  const GMAX = 6;
  let gaugeBuilt = "";
  function buildGauge(th) {
    const key = `${th.caution_enter}|${th.halt_enter}`;
    if (key === gaugeBuilt) return;
    gaugeBuilt = key;
    const svg = $("#gauge");
    const cx = 120, cy = 124, r = 92;
    const ang = (v) => 180 - (clamp(v, 0, GMAX) / GMAX) * 180;
    const pt = (a, rr) => [cx + rr * Math.cos((a * Math.PI) / 180), cy - rr * Math.sin((a * Math.PI) / 180)];
    const arc = (a0, a1, rr) => {
      const [x0, y0] = pt(a0, rr), [x1, y1] = pt(a1, rr);
      return `M${x0.toFixed(2)} ${y0.toFixed(2)} A${rr} ${rr} 0 0 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
    };
    const zones = [[0, th.caution_enter, "--good"], [th.caution_enter, th.halt_enter, "--warning"], [th.halt_enter, GMAX, "--critical"]];
    const NS = "http://www.w3.org/2000/svg";
    const el = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); return e; };
    svg.replaceChildren();
    zones.forEach(([a, b, c], i) => {
      const gapA = i ? 0.9 : 0, gapB = i < 2 ? 0.9 : 0;
      svg.append(el("path", { class: "zone", d: arc(ang(a) - gapA, ang(b) + gapB, r + 16), style: `stroke: var(${c})` }));
    });
    svg.append(el("path", { d: arc(180, 0, r), fill: "none", style: "stroke: var(--surface-3)", "stroke-width": "15", "stroke-linecap": "round" }));
    svg.append(el("path", { class: "val", id: "gaugeVal", d: arc(180, 0, r), pathLength: "100" }));
    for (const v of [0, th.caution_enter, th.halt_enter, GMAX]) {
      const [x, y] = pt(ang(v), r + 32);
      const t = el("text", { x: x.toFixed(1), y: (y + 4).toFixed(1), "text-anchor": v === 0 ? "end" : v === GMAX ? "start" : "middle" });
      t.textContent = v === GMAX ? `${GMAX}+` : String(v);
      svg.append(t);
    }
    const g = el("g", { class: "needle", id: "gaugeNeedle", style: "transform: rotate(-90deg)" });
    g.append(el("line", { x1: cx, y1: cy, x2: cx, y2: cy - r + 14 }));
    svg.append(g);
    svg.append(el("circle", { class: "hub", cx, cy, r: 9 }));
    svg.append(el("circle", { class: "hub-ring", cx, cy, r: 3.5 }));
  }
  function renderGauge(s) {
    const th = s.thresholds;
    buildGauge(th);
    const v = s.risk_score;
    const needle = $("#gaugeNeedle"), val = $("#gaugeVal"), num = $("#gaugeNum"), word = $("#gaugeWord");
    if (v == null || !s.monitor_ready) {
      needle.style.transform = "rotate(-90deg)";
      val.style.strokeDashoffset = "100";
      setText(num, "–");
      num.dataset.v = "";
      setText(word, "Not ready");
      word.dataset.tone = "na";
      return;
    }
    const frac = clamp(v / GMAX, 0, 1);
    needle.style.transform = `rotate(${(frac * 180 - 90).toFixed(2)}deg)`;
    val.style.strokeDashoffset = String((100 - frac * 100).toFixed(2));
    const tone = v >= th.halt_enter ? "critical" : v >= th.caution_enter ? "warning" : "good";
    val.style.stroke = `var(--${tone})`;
    tween(num, v, 2);
    setText(word, tone === "critical" ? "High" : tone === "warning" ? "Elevated" : "Low");
    word.dataset.tone = tone;
  }

  function renderPerms(ul, state, frac) {
    const p = permsFor(state, frac);
    if (!ul.children.length) {
      for (const [k, label] of [["open", "Open new positions"], ["reduce", "Reduce or close positions"]]) {
        ul.append(h("li", { dataset: { perm: k } }, h("span", { class: "pi" }), h("span", { text: label }), h("span", { class: "state-word" })));
      }
    }
    for (const li of ul.children) {
      const ok = p[li.dataset.perm];
      if (li.dataset.ok !== String(ok)) {
        li.dataset.ok = String(ok);
        li.querySelector(".pi").innerHTML = icon(ok ? "check" : "x");
        setText(li.querySelector(".state-word"), ok ? "Allowed" : "Blocked");
      }
    }
    return p;
  }

  function renderMiniSignals(s) {
    const box = $("#miniSignals");
    if (!box.children.length) {
      for (const sig of SIGNALS) {
        box.append(h("div", { class: "ms-row", dataset: { sig: sig.key } },
          h("div", { class: "ms-name" }, h("span", { class: "sw", style: { background: `var(${sig.color})` } }),
            h("span", null, sig.name, h("small", { text: sig.q }))),
          h("div", { class: "ms-track" }, h("span"),
            h("i", { class: "tick", style: { left: "20%" } }), h("i", { class: "tick", style: { left: "40%" } }), h("i", { class: "tick", style: { left: "70%" } })),
          h("div", { class: "ms-level" }, h("span", { class: "pill" }), h("span", { class: "x mono z" }))));
      }
    }
    for (const sig of SIGNALS) {
      const row = $(`.ms-row[data-sig="${sig.key}"]`, box);
      const c = s.components[sig.key];
      const z = c && c.available ? c.z : null;
      const t = zTone(z);
      const bar = $(".ms-track > span", row);
      bar.style.width = `${z == null ? 0 : clamp(z / 5, 0.02, 1) * 100}%`;
      bar.style.background = `var(--${t.tone === "na" ? "axis" : t.tone})`;
      const pill = $(".pill", row);
      setText(pill, t.word);
      pill.dataset.tone = t.tone;
      setText($(".z", row), z == null ? " z –" : ` z ${fmt(z, 2)}`);
    }
  }

  function changeItem(t) {
    const to = level(t.new_state), from = level(t.prev_state);
    return h("li", null,
      h("span", { class: "change-dot", dataset: { state: t.new_state }, style: { background: `var(--${to.tone})` }, html: icon(to.icon) }),
      h("div", { class: "change-head" }, h("span", { text: from.title }), h("span", { class: "arrow", text: "→" }),
        h("span", { text: to.title }), h("span", { class: "change-time", text: fmtFull(t.as_of) })),
      h("div", { class: "change-text" }, storyText(t), h("span", { class: "x", text: `${t.rule}: ${t.reason}` })));
  }
  function renderChanges() {
    const key = S.transitions.map((t) => t.as_of + t.new_state).join("|") + S.runId;
    if (key === S.transKey) return;
    S.transKey = key;
    const latest = $("#latestChanges"), story = $("#story");
    if (!S.transitions.length) {
      latest.replaceChildren(h("li", { class: "empty", text: "No level changes yet." }));
      story.replaceChildren(h("li", { class: "empty", text: "No level changes yet." }));
      return;
    }
    latest.replaceChildren(...S.transitions.slice(0, 4).map(changeItem));
    story.replaceChildren(...S.transitions.map(changeItem));
  }

  // ================================================================ charts
  class TimeChart {
    constructor(canvas, opts) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.o = Object.assign({ axes: true, bands: true, thresholds: true, scenarios: true, hover: true, area: false, endDot: null }, opts);
      this.rows = [];
      this.ts = [];
      this.series = [];
      this.progress = 1;
      this.hoverIdx = null;
      this.xmin = 0;
      this.xmax = 1;
      this.view = null; // zoomed window {from, to, follow}, or null for the full range
      if (this.o.hover) {
        canvas.addEventListener("pointermove", (e) => (this.drag ? this.onDrag(e) : this.onMove(e)));
        canvas.addEventListener("pointerleave", () => { if (!this.drag) this.onLeave(); });
      }
      if (this.o.zoom) {
        canvas.tabIndex = 0;
        canvas.addEventListener("wheel", (e) => { e.preventDefault(); this.zoomAt(e.clientX, e.deltaY > 0 ? 1.25 : 0.8); }, { passive: false });
        canvas.addEventListener("pointerdown", (e) => {
          if (e.button !== 0 || !this.geom) return;
          this.drag = { x: e.clientX, from: this.xmin, to: this.xmax, moved: false };
          canvas.setPointerCapture(e.pointerId);
        });
        canvas.addEventListener("pointerup", () => { this.drag = null; });
        canvas.addEventListener("dblclick", () => this.resetZoom());
        canvas.addEventListener("keydown", (e) => this.onKey(e));
      }
      new ResizeObserver(() => this.draw()).observe(canvas.parentElement);
    }
    set(rows, series, xmin, xmax) {
      this.rows = rows;
      this.ts = rows.map((r) => Date.parse(r.as_of));
      this.series = series;
      this.bx0 = xmin != null ? xmin : this.ts[0];
      this.bx1 = xmax != null ? xmax : this.ts[this.ts.length - 1];
      this.applyView();
    }
    applyView() {
      let a = this.bx0, b = this.bx1;
      if (this.view && b > a) {
        const full = b - a;
        const span = Math.min(this.view.to - this.view.from, full);
        let from = this.view.follow ? b - span : this.view.from;
        from = clamp(from, a, b - span);
        if (span >= full - 1000) this.view = null; // zoomed all the way out
        else { this.view = { from, to: from + span, follow: from + span >= b - 1000 }; a = from; b = from + span; }
      }
      this.xmin = a;
      this.xmax = b > a ? b : a + 1;
      if (this.o.onView) this.o.onView(!!this.view);
    }
    timeAt(clientX) {
      const { x0, x1 } = this.geom;
      const x = clientX - this.canvas.getBoundingClientRect().left;
      return this.xmin + ((clamp(x, x0, x1) - x0) / (x1 - x0)) * (this.xmax - this.xmin);
    }
    zoomAt(clientX, factor) {
      if (!this.rows.length || !this.geom) return;
      const t = this.timeAt(clientX), span = this.xmax - this.xmin;
      const next = clamp(span * factor, 3600e3, this.bx1 - this.bx0); // never closer than one hour
      const from = t - ((t - this.xmin) / span) * next;
      this.view = { from, to: from + next, follow: false };
      this.applyView();
      this.draw();
      if (this.hoverIdx != null) this.tipAt(this.hoverIdx);
    }
    resetZoom() { this.view = null; this.applyView(); this.draw(); }
    onDrag(e) {
      const { x0, x1 } = this.geom, d = this.drag;
      if (Math.abs(e.clientX - d.x) > 3) d.moved = true;
      if (!d.moved) return;
      const shift = -((e.clientX - d.x) / (x1 - x0)) * (d.to - d.from);
      this.view = { from: d.from + shift, to: d.to + shift, follow: false };
      this.applyView();
      this.hoverIdx = null;
      $("#chartTip").hidden = true;
      this.draw();
    }
    onKey(e) {
      if (!this.rows.length) return;
      if (e.key === "Escape") { this.onLeave(); return; }
      const step = { ArrowLeft: -1, ArrowRight: 1 }[e.key];
      if (!(e.key === "Home" || e.key === "End" || step)) return;
      e.preventDefault();
      let i = this.hoverIdx;
      if (e.key === "Home") i = 0;
      else if (e.key === "End" || i == null) i = this.rows.length - 1;
      else i = clamp(i + step * (e.shiftKey ? 12 : 1), 0, this.rows.length - 1); // Shift = one hour
      const t = this.ts[i], span = this.xmax - this.xmin;
      if (t < this.xmin || t > this.xmax) { // keep the selected reading on screen
        this.view = { from: t - span / 2, to: t + span / 2, follow: false };
        this.applyView();
      }
      this.hoverIdx = i;
      this.draw();
      this.tipAt(i);
    }
    tipAt(i) {
      if (!this.geom) return;
      const rect = this.canvas.getBoundingClientRect();
      showTip({ clientX: rect.left + this.geom.X(this.ts[i]), clientY: rect.top + 60 }, this.rows[i], this.series);
    }
    animateIn() {
      if (reduceMotion) { this.progress = 1; this.draw(); return; }
      const t0 = performance.now();
      cancelAnimationFrame(this._raf);
      const step = (t) => {
        const k = Math.min(1, (t - t0) / 1000);
        this.progress = 1 - Math.pow(1 - k, 3);
        this.draw();
        if (k < 1) this._raf = requestAnimationFrame(step);
      };
      this._raf = requestAnimationFrame(step);
    }
    size() {
      const rect = this.canvas.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const w = Math.max(1, Math.round(rect.width * dpr)), hh = Math.max(1, Math.round(rect.height * dpr));
      if (this.canvas.width !== w || this.canvas.height !== hh) { this.canvas.width = w; this.canvas.height = hh; }
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      return [rect.width, rect.height];
    }
    draw() {
      if (!this.canvas.offsetParent) return; // hidden tab
      const [W, H] = this.size();
      const ctx = this.ctx;
      ctx.clearRect(0, 0, W, H);
      const ink = css("--ink"), axisInk = css("--axis-ink"), grid = css("--grid"), surface = css("--surface");
      if (!this.rows.length) {
        ctx.fillStyle = axisInk;
        ctx.font = `13px ${css("--font") || "system-ui"}`;
        ctx.textAlign = "center";
        ctx.fillText("Waiting for data…", W / 2, H / 2);
        return;
      }
      const [pt, pr, pb, pl] = this.o.axes ? [14, 16, 26, 36] : [5, 6, 5, 4];
      const x0 = pl, x1 = W - pr, y0 = pt, y1 = H - pb;
      const X = (t) => x0 + ((t - this.xmin) / (this.xmax - this.xmin)) * (x1 - x0);
      const th = S.status ? S.status.thresholds : { caution_enter: 2, halt_enter: 3.5 };
      let ymax = this.o.ymax || 0;
      if (!ymax) {
        ymax = this.o.thresholds ? th.halt_enter + 1 : 3;
        for (const sr of this.series) for (const r of this.rows) { const v = r[sr.key]; if (v != null && v > ymax) ymax = v; }
        ymax = Math.min(12, Math.ceil(ymax));
      }
      const Y = (v) => y1 - (clamp(v, 0, ymax) / ymax) * (y1 - y0);
      const bandA = parseFloat(css("--band-alpha")) || 0.15;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";

      // level bands
      if (this.o.bands) {
        let i = 0;
        while (i < this.rows.length) {
          let j = i;
          while (j + 1 < this.rows.length && this.rows[j + 1].state === this.rows[i].state) j++;
          const xa = X(this.ts[i]), xb = j + 1 < this.rows.length ? X(this.ts[j + 1]) : X(this.xmax);
          ctx.fillStyle = withAlpha(css(`--${level(this.rows[i].state).tone}`), bandA);
          ctx.fillRect(Math.max(x0, xa), y0, Math.max(0, Math.min(x1, xb) - Math.max(x0, xa)), y1 - y0);
          i = j + 1;
        }
      }
      // labelled scenario windows
      if (this.o.scenarios && S.status) {
        ctx.font = `600 11px ${css("--font")}`;
        ctx.textAlign = "left";
        for (const sc of S.status.replay.scenarios || []) {
          const a = Date.parse(sc.onset), b = Date.parse(sc.end);
          if (b < this.xmin || a > this.xmax) continue;
          const xa = Math.max(x0, X(a)), xb = Math.min(x1, X(b));
          ctx.fillStyle = withAlpha(ink.startsWith("#") ? ink : "#888888", 0.05);
          ctx.fillRect(xa, y0, xb - xa, y1 - y0);
          ctx.strokeStyle = withAlpha(ink.startsWith("#") ? ink : "#888888", 0.25);
          ctx.lineWidth = 1;
          ctx.beginPath(); ctx.moveTo(xa + 0.5, y0); ctx.lineTo(xa + 0.5, y1); ctx.stroke();
          ctx.fillStyle = axisInk;
          const label = scenarioCopy(sc).title + (S.status.synthetic ? " (invented)" : "");
          const tw = ctx.measureText(label).width;
          if (this.o.axes && tw + 10 < x1 - x0) ctx.fillText(label, clamp(xa + 5, x0 + 4, x1 - tw - 4), y0 + 12);
        }
      }
      // grid + axes
      if (this.o.axes) {
        ctx.font = `11px ${css("--font")}`;
        ctx.fillStyle = axisInk;
        ctx.strokeStyle = grid;
        ctx.lineWidth = 1;
        ctx.textAlign = "right";
        const stepY = ymax <= 6 ? 1 : 2;
        for (let v = 0; v <= ymax + 1e-9; v += stepY) {
          const y = Math.round(Y(v)) + 0.5;
          ctx.beginPath(); ctx.moveTo(x0, y); ctx.lineTo(x1, y); ctx.stroke();
          ctx.fillText(String(v), x0 - 7, y + 4);
        }
        const span = this.xmax - this.xmin, HR = 3600e3;
        const steps = [HR, 2 * HR, 3 * HR, 6 * HR, 12 * HR, 24 * HR, 48 * HR, 72 * HR, 168 * HR];
        const maxTicks = Math.max(2, Math.floor((x1 - x0) / 96));
        const st = steps.find((s) => span / s <= maxTicks) || steps[steps.length - 1];
        ctx.textAlign = "center";
        for (let t = Math.ceil(this.xmin / st) * st; t <= this.xmax; t += st) {
          const x = X(t);
          if (x < x0 + 18 || x > x1 - 18) continue;
          const iso = new Date(t).toISOString();
          const lbl = st >= 24 * HR || fmtClock(iso) === "00:00" ? fmtDay(iso) : fmtClock(iso);
          ctx.fillText(lbl, x, H - 8);
        }
      }
      // thresholds
      if (this.o.thresholds) {
        ctx.save();
        ctx.setLineDash([5, 5]);
        ctx.lineWidth = 1.2;
        ctx.font = `600 11px ${css("--font")}`;
        ctx.textAlign = "left";
        for (const [v, tone, label] of [[th.caution_enter, "warning", "Be careful"], [th.halt_enter, "critical", "Stop"]]) {
          if (v > ymax) continue;
          const y = Math.round(Y(v)) + 0.5;
          ctx.strokeStyle = css(`--${tone}`);
          ctx.beginPath(); ctx.moveTo(x0, y); ctx.lineTo(x1, y); ctx.stroke();
          if (this.o.axes) { ctx.fillStyle = axisInk; ctx.fillText(label, x0 + 6, y - 5); }
        }
        ctx.restore();
      }
      // series (clipped for the draw-in animation)
      ctx.save();
      ctx.beginPath();
      ctx.rect(x0 - 2, 0, (x1 - x0 + 4) * this.progress, H);
      ctx.clip();
      for (const sr of this.series) {
        const color = sr.color.startsWith("--") ? css(sr.color) : sr.color;
        ctx.strokeStyle = color;
        ctx.lineWidth = sr.width || 2;
        ctx.beginPath();
        let pen = false, firstX = null, lastX = null;
        this.rows.forEach((r, i) => {
          const v = r[sr.key];
          if (v == null) { pen = false; return; }
          const x = X(this.ts[i]), y = Y(v);
          if (pen) ctx.lineTo(x, y); else { ctx.moveTo(x, y); if (firstX == null) firstX = x; }
          pen = true;
          lastX = x;
        });
        ctx.stroke();
        if (this.o.area && sr.area && firstX != null) {
          ctx.lineTo(lastX, y1);
          ctx.lineTo(firstX, y1);
          ctx.closePath();
          const g = ctx.createLinearGradient(0, y0, 0, y1);
          g.addColorStop(0, withAlpha(color, 0.18));
          g.addColorStop(1, withAlpha(color, 0));
          ctx.fillStyle = g;
          ctx.fill();
        }
      }
      ctx.restore();
      // end dot (sparklines)
      if (this.o.endDot && this.progress >= 1) {
        for (let i = this.rows.length - 1; i >= 0; i--) {
          const v = this.rows[i][this.series[0].key];
          if (v == null) continue;
          const x = X(this.ts[i]), y = Y(v);
          ctx.fillStyle = surface;
          ctx.beginPath(); ctx.arc(x, y, 6, 0, Math.PI * 2); ctx.fill();
          ctx.fillStyle = css(this.o.endDot);
          ctx.beginPath(); ctx.arc(x, y, 4, 0, Math.PI * 2); ctx.fill();
          break;
        }
      }
      // crosshair
      if (this.hoverIdx != null && this.rows[this.hoverIdx]) {
        const r = this.rows[this.hoverIdx], x = Math.round(X(this.ts[this.hoverIdx])) + 0.5;
        ctx.strokeStyle = css("--axis");
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(x, y0); ctx.lineTo(x, y1); ctx.stroke();
        for (const sr of this.series) {
          const v = r[sr.key];
          if (v == null) continue;
          const y = Y(v);
          ctx.fillStyle = surface;
          ctx.beginPath(); ctx.arc(x, y, 6, 0, Math.PI * 2); ctx.fill();
          ctx.fillStyle = sr.color.startsWith("--") ? css(sr.color) : sr.color;
          ctx.beginPath(); ctx.arc(x, y, 4, 0, Math.PI * 2); ctx.fill();
        }
      }
      this.geom = { x0, x1, X };
    }
    onMove(e) {
      if (!this.rows.length || !this.geom) return;
      const t = this.timeAt(e.clientX);
      let lo = 0, hi = this.ts.length - 1;
      while (lo < hi) { const mid = (lo + hi) >> 1; if (this.ts[mid] < t) lo = mid + 1; else hi = mid; }
      if (lo > 0 && Math.abs(this.ts[lo - 1] - t) < Math.abs(this.ts[lo] - t)) lo -= 1;
      this.hoverIdx = lo;
      this.draw();
      showTip(e, this.rows[lo], this.series);
    }
    onLeave() {
      this.hoverIdx = null;
      this.draw();
      $("#chartTip").hidden = true;
    }
  }

  function hideTips() {
    $("#chartTip").hidden = true;
    for (const c of [miniChart, bigChart, ...Object.values(sparks)]) {
      if (c.hoverIdx != null) { c.hoverIdx = null; c.draw(); }
    }
  }

  const SERIES_NAMES = { risk_score: "Risk score", panic_z: "Negativity", drift_z: "Topic change", volume_z: "News volume" };
  function showTip(e, r, series) {
    const tip = $("#chartTip");
    const L = level(r.state);
    const rows = [
      h("div", { class: "tt-time", text: `${fmtFull(r.as_of)}${S.status && S.status.mode === "replay" ? " · simulated" : ""}` }),
      h("div", { class: "tt-row" }, h("span", { class: "k", style: { background: `var(--${L.tone})`, height: "10px", width: "10px", "border-radius": "3px" } }),
        h("b", { text: L.title }), h("span", { text: r.monitor_ready ? (r.degraded ? "level · missing data" : "level") : "level · not ready" })),
    ];
    for (const sr of series) {
      rows.push(h("div", { class: "tt-row" }, h("span", { class: "k", style: { background: sr.color.startsWith("--") ? `var(${sr.color})` : sr.color } }),
        h("b", { text: fmt(r[sr.key], 2) }), h("span", { text: SERIES_NAMES[sr.key] || sr.key })));
    }
    if (r.cusum_alarm_active) rows.push(h("div", { class: "tt-row" }, h("span", { html: icon("bolt") }), h("span", { text: "sudden-change alarm" })));
    tip.replaceChildren(...rows);
    tip.hidden = false;
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    let x = e.clientX + 16, y = e.clientY + 16;
    if (x + tw > window.innerWidth - 8) x = e.clientX - tw - 16;
    if (y + th > window.innerHeight - 8) y = e.clientY - th - 16;
    tip.style.left = `${x}px`;
    tip.style.top = `${y}px`;
  }

  const miniChart = new TimeChart($("#miniChart"), { area: true });
  const bigChart = new TimeChart($("#bigChart"), { zoom: true, onView: (z) => { const b = $("#zoomReset"); if (b) b.hidden = !z; } });
  const sparks = {};

  function lastHours(hours) {
    if (!S.history.length) return [];
    if (!hours) return S.history;
    const end = Date.parse(S.history[S.history.length - 1].as_of);
    const from = end - hours * 3600e3;
    let lo = 0, hi = S.history.length;
    while (lo < hi) { const m = (lo + hi) >> 1; if (Date.parse(S.history[m].as_of) < from) lo = m + 1; else hi = m; }
    return S.history.slice(lo);
  }

  let chartsAnimated = { overview: false, signals: false, timeline: false };
  function renderCharts(tabChanged) {
    const tab = S.tab;
    if (tab === "overview") {
      const rows = lastHours(24);
      const end = rows.length ? Date.parse(rows[rows.length - 1].as_of) : null;
      miniChart.set(rows, [{ key: "risk_score", color: "--ink", width: 2, area: true }], end ? end - 24 * 3600e3 : null, end);
      if (tabChanged || !chartsAnimated.overview) { if (rows.length) chartsAnimated.overview = true; miniChart.animateIn(); } else miniChart.draw();
    } else if (tab === "timeline") {
      const rows = lastHours(S.range);
      const series = [{ key: "risk_score", color: "--ink", width: 2.25 }];
      if (S.series.panic_z) series.push({ key: "panic_z", color: "--s1" });
      if (S.series.drift_z) series.push({ key: "drift_z", color: "--s2" });
      if (S.series.volume_z) series.push({ key: "volume_z", color: "--s3" });
      const end = rows.length ? Date.parse(rows[rows.length - 1].as_of) : null;
      bigChart.set(rows, series, S.range && end ? end - S.range * 3600e3 : null, end);
      if (tabChanged || !chartsAnimated.timeline) { if (rows.length) chartsAnimated.timeline = true; bigChart.animateIn(); } else bigChart.draw();
      renderBigLegend(series);
      if (S.table) renderTable(rows);
    } else if (tab === "signals") {
      const rows = lastHours(24);
      const end = rows.length ? Date.parse(rows[rows.length - 1].as_of) : null;
      for (const sig of SIGNALS) {
        const c = sparks[sig.key];
        if (!c) continue;
        c.set(rows, [{ key: sig.z, color: "--axis-ink", width: 1.6 }], end ? end - 24 * 3600e3 : null, end);
        if (tabChanged || !chartsAnimated.signals) c.animateIn(); else c.draw();
      }
      if (rows.length) chartsAnimated.signals = true;
    }
  }

  function legendBands() {
    return ["NORMAL", "CAUTION", "HALT"].map((st) => {
      const L = level(st);
      return h("span", { class: "lk" }, h("span", { class: "band", style: { background: `var(--${L.tone})` }, html: icon(L.icon) }), `${L.title} (shading)`);
    });
  }
  function renderMiniLegend() {
    const box = $("#miniLegend");
    if (box.children.length) return;
    box.append(h("span", { class: "lk" }, h("span", { class: "line" }), "Risk score"), ...legendBands(),
      h("span", { class: "lk" }, h("span", { class: "dash" }), "Thresholds"));
  }
  function renderBigLegend(series) {
    const key = series.map((s) => s.key).join(",");
    const box = $("#bigLegend");
    if (box.dataset.key === key) return;
    box.dataset.key = key;
    box.replaceChildren(...series.map((sr) => h("span", { class: "lk" },
      h("span", { class: "line", style: { background: `var(${sr.color})` } }), SERIES_NAMES[sr.key])),
    ...legendBands(), h("span", { class: "lk" }, h("span", { class: "dash" }), "Be careful / Stop thresholds"));
  }
  // Every reading in the selected range, newest first. Rows are plain numbers and our own labels,
  // so they are built as one HTML string; new readings are prepended instead of rebuilding.
  function tableRow(r) {
    return `<tr><td>${fmtFull(r.as_of)}</td><td>${level(r.state).title}</td><td class="num">${fmt(r.risk_score)}</td>`
      + `<td class="num">${fmt(r.panic_z)}</td><td class="num">${fmt(r.drift_z)}</td><td class="num">${fmt(r.volume_z)}</td></tr>`;
  }
  function renderTable(rows) {
    const box = $("#tableView");
    const rk = `${S.runId}|${S.range}`;
    const oldest = rows.length ? rows[0].as_of : "", newest = rows.length ? rows[rows.length - 1].as_of : "";
    const tbody = box.querySelector("tbody");
    if (tbody && box.dataset.rk === rk && box.dataset.oldest === oldest) {
      if (box.dataset.newest === newest) return;
      const fresh = [];
      for (let i = rows.length - 1; i >= 0 && rows[i].as_of > box.dataset.newest; i--) fresh.push(tableRow(rows[i]));
      tbody.insertAdjacentHTML("afterbegin", fresh.join(""));
    } else {
      const head = ["Time (UTC)", "Level", "Risk score", "Negativity", "Topic change", "News volume"].map((t) => `<th>${t}</th>`).join("");
      const body = [];
      for (let i = rows.length - 1; i >= 0; i--) body.push(tableRow(rows[i]));
      box.innerHTML = `<p class="caption top table-count"></p><table><thead><tr>${head}</tr></thead><tbody>${body.join("")}</tbody></table>`;
    }
    Object.assign(box.dataset, { rk, oldest, newest });
    setText($(".table-count", box), `Every 5-minute reading in this range, newest first (${rows.length.toLocaleString("en-GB")} rows).`);
  }

  // ================================================================ signals tab
  function buildSignalCards() {
    const box = $("#signalCards");
    for (const sig of SIGNALS) {
      const canvas = h("canvas");
      const card = h("article", { class: "card signal", dataset: { sig: sig.key } },
        h("div", { class: "sig-head" }, h("span", { class: "sig-ico", style: { background: `var(${sig.color})` }, html: icon(sig.icon) }),
          h("div", null, h("h3", { text: sig.name }), h("p", { class: "q", text: sig.q }))),
        h("div", { class: "ring-meter", html: '<svg viewBox="0 0 120 120" aria-hidden="true"><circle class="track" cx="60" cy="60" r="50"/>'
          + '<circle class="val" cx="60" cy="60" r="50" pathLength="100"/></svg><div class="ring-center"><b class="lvl">–</b><span class="zline"></span></div>' }),
        h("div", null, h("div", { class: "spark-box" }, canvas), h("div", { class: "spark-label" }, h("span", { text: "24 h ago" }), h("span", { text: "now" }))),
        h("p", { class: "explain", text: sig.explain }),
        h("dl", { class: "details x" }));
      box.append(card);
      sparks[sig.key] = new TimeChart(canvas, { axes: false, bands: false, thresholds: false, scenarios: false, hover: true, endDot: sig.color, ymax: 0 });
    }
  }
  function renderSignals(s) {
    for (const sig of SIGNALS) {
      const card = $(`.signal[data-sig="${sig.key}"]`);
      if (!card) continue;
      const c = s.components[sig.key] || {};
      const z = c.available ? c.z : null;
      const t = zTone(z);
      const ring = $(".ring-meter .val", card);
      ring.style.strokeDashoffset = String(100 - (z == null ? 0 : clamp(z / 5, 0.01, 1) * 100));
      ring.style.stroke = `var(--${t.tone === "na" ? "axis" : t.tone})`;
      setText($(".lvl", card), t.word);
      setText($(".zline", card), z == null ? (c.reason ? "can't measure yet" : "") : `score ${fmt(z, 1)} · 0 = typical`);
      const dl = $(".details", card);
      const rows = [
        ["raw value", fmt(c.raw, sig.key === "volume" ? 0 : 3)], ["z (clipped)", fmt(c.z, 2)], ["z (unclipped)", fmt(c.z_unclipped, 2)],
        ["samples", c.sample_count ?? "–"], ["baseline", `${c.baseline_ready ? "ready" : "warming up"} · n=${c.baseline_count ?? "–"} · centre ${fmt(c.baseline_center, 3)} · scale ${fmt(c.baseline_scale, 3)}`],
        ["model", `${c.implementation || "–"}${c.is_fallback ? " (fallback)" : ""}`],
      ];
      if (c.reason) rows.push(["note", c.reason]);
      const key = JSON.stringify(rows);
      if (dl.dataset.key !== key) {
        dl.dataset.key = key;
        dl.replaceChildren(...rows.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: String(v) })]));
      }
    }
    // contribution bar
    const box = $("#contrib");
    const parts = SIGNALS.map((sig) => {
      const c = s.components[sig.key];
      const z = c && c.available && c.z > 0 ? c.z : 0;
      return { sig, v: (s.weights[sig.key] || 0) * z };
    });
    const total = parts.reduce((a, p) => a + p.v, 0);
    const scale = Math.max(6, total);
    if (!box.children.length) {
      for (const p of parts) box.append(h("div", { class: "seg", dataset: { sig: p.sig.key }, style: { background: `var(${p.sig.color})` } }));
      box.append(h("div", { class: "rest" }));
      $("#contribLegend").replaceChildren(...parts.map((p) => h("span", { class: "lk" }, h("span", { class: "band", style: { background: `var(${p.sig.color})` } }), h("span", { class: "cv", dataset: { sig: p.sig.key } }))));
    }
    const W = box.clientWidth || 600;
    for (const p of parts) {
      const seg = $(`.seg[data-sig="${p.sig.key}"]`, box);
      seg.style.flexGrow = String(p.v);
      seg.classList.toggle("zero", p.v <= 0.0001);
      setText(seg, (p.v / scale) * W > 48 ? fmt(p.v, 2) : "");
      setText($(`.cv[data-sig="${p.sig.key}"]`), `${p.sig.name} ${fmt(p.v, 2)}`);
    }
    const rest = $(".rest", box);
    rest.style.flexGrow = String(Math.max(0, scale - total));
    setText(rest, total <= 0.0001 ? "Nothing unusual — every signal is at or below normal" : "");
  }

  // ================================================================ news tab
  function tile(label, value, note, hot) {
    return h("div", { class: `tile${hot ? " hot" : ""}` }, h("div", { class: "t-label", text: label }), h("div", { class: "t-value", text: value }), h("div", { class: "t-note", text: note }));
  }
  function renderNews(s) {
    const c = s.counters;
    const healthy = s.adapters.filter((a) => a.last_success && !a.consecutive_failures).length;
    const tiles = [
      ["Headlines read", c.headlines.toLocaleString("en-GB"), "unique stories so far"],
      ["Copies ignored", c.duplicates.toLocaleString("en-GB"), "same story, another outlet"],
      ["Broken records skipped", c.malformed.toLocaleString("en-GB"), "missing text or time"],
      ["News sources", `${healthy} / ${s.adapters.length}`, "working right now"],
    ];
    const key = JSON.stringify(tiles);
    if (key !== S.tilesKey) {
      S.tilesKey = key;
      $("#newsTiles").replaceChildren(...tiles.map(([a, b, n]) => tile(a, b, n)));
    }
    const fresh = $("#newsFresh");
    if (fresh) {
      const g = s.adapters.find((a) => a.data_through);
      let txt = "";
      if (s.mode === "live") {
        txt = `Live: news assessed up to ${s.as_of ? fmtClock(s.as_of) : "–"} UTC. `;
        if (g) txt += `GDELT is checked every ${Math.round(g.poll_seconds / 60)} min and publishes a new batch every 15 min`
          + (g.late_files ? ` (${g.late_files} late batch${g.late_files > 1 ? "es" : ""} still awaited).` : ".");
        txt += " Central-bank feeds only change when they publish (a few times a day).";
      }
      setText(fresh, txt);
      fresh.hidden = !txt;
    }
    const heads = s.top_headlines || [];
    const focus = s.focus && s.focus.text ? s.focus.text : "";
    setText($("#newsCaption"), focus
      ? `The headlines pushing negativity up most in the last hour, now weighted by how closely they match “${focus}”. A longer, redder bar means more negative; the tag shows how on-topic each one is.`
      : "The most negative headlines from the last hour, as scored by the AI. A longer, redder bar means the headline sounds more negative.");
    const hk = focus + "|" + heads.map((x) => `${x.headline_id}:${x.relevance == null ? "" : x.relevance.toFixed(2)}`).join("|");
    if (hk === S.headKey) return;
    S.headKey = hk;
    const ul = $("#headlines");
    if (!heads.length) {
      ul.replaceChildren(h("li", { class: "empty", text: "No headlines in the last hour yet." }));
      return;
    }
    ul.replaceChildren(...heads.map((x, i) => {
      const p = x.p_negative == null ? null : x.p_negative;
      const tone = p == null ? "na" : p >= 0.65 ? "critical" : p >= 0.35 ? "warning" : "good";
      const word = p == null ? "not scored" : p >= 0.65 ? "Negative" : p >= 0.35 ? "Mixed" : "Calm";
      const rel = x.relevance;
      return h("li", { style: { "--i": String(i) } },
        h("div", { class: "hl-text" }, x.headline, rel == null ? null : relTag(rel, s.focus)),
        h("div", { class: "neg" },
          h("div", { class: "neg-label" }, h("span", { text: word }), h("b", { text: p == null ? "–" : `${Math.round(p * 100)}%` })),
          h("div", { class: "neg-track" }, h("span", { style: { width: `${p == null ? 0 : Math.max(3, p * 100)}%`, background: `var(--${tone === "na" ? "axis" : tone})` } }))),
        h("div", { class: "hl-meta" }, h("span", { text: x.source }), h("span", { text: fmtFull(x.event_time) }),
          h("span", { class: "x mono", text: `${x.headline_id} · ${x.sentiment_impl || ""}` })));
    }));
  }

  // Same bands as the Optimize-for dialog: off topic < 10% ≤ slightly < 50% ≤ mostly < 90% ≤ clearly.
  function relTag(rel, focus) {
    const b = focus && focus.background_weight != null ? focus.background_weight : 0.05;
    const w = b + (1 - b) * rel;
    return h("span", {
      class: "rel-tag", dataset: { level: rel >= 0.9 ? "high" : rel >= 0.5 ? "mid" : rel >= 0.1 ? "low" : "off" },
      text: rel >= 0.1 ? `${Math.round(rel * 100)}% on topic` : "off topic",
      title: `Relevance to your focus ${Math.round(rel * 100)}%: this headline counts ${w.toFixed(2)}× as much as a fully matching one.`,
    });
  }

  // ================================================================ optimize for (focus)
  const FOCUS_PRESETS = [
    { icon: "globe", label: "Whole market", text: "" },
    { icon: "oil", label: "Oil & energy", text: "OPEC, crude oil prices, oil supply, refineries, oil tankers, natural gas" },
    { icon: "bank", label: "Banks & credit", text: "banks, bank run, deposits, credit markets, loan defaults, bank capital" },
    { icon: "scale", label: "Rates & central banks", text: "central bank, interest rates, inflation, Federal Reserve, bond yields" },
    { icon: "cpu", label: "Tech & chips", text: "semiconductors, chip makers, AI stocks, big tech earnings" },
    { icon: "coin", label: "Crypto", text: "bitcoin, crypto exchange, stablecoin, crypto regulation" },
  ];
  let focusTimer = 0, focusSeq = 0;
  const pct = (x, d = 0) => `${(x * 100).toFixed(d)}%`;
  function focusBody() {
    return { text: $("#focusText").value.trim(), background_weight: Number($("#focusBg").value) / 100 };
  }
  function renderFocusBtn(s) {
    const f = s.focus || {};
    const b = $("#focusBtn");
    b.classList.toggle("on", !!f.text);
    setText($("#focusName"), f.text ? f.phrases.slice(0, 2).join(", ") + (f.phrases.length > 2 ? "…" : "") : "Whole market");
    b.title = f.text ? `Optimized for: ${f.text} (click to change)` : "Choose which news matters most for what you trade";
  }
  function markPreset() {
    const t = $("#focusText").value.trim().toLowerCase();
    for (const b of $$("#focusPresets button")) b.classList.toggle("on", b.dataset.text.toLowerCase() === t);
  }
  function openFocus() {
    const f = (S.status && S.status.focus) || {};
    $("#focusText").value = f.text || "";
    $("#focusBg").value = String(Math.round((f.background_weight ?? 0.05) * 100));
    setText($("#focusBgVal"), `${$("#focusBg").value}%`);
    markPreset();
    $("#focusDlg").showModal();
    previewFocus();
  }
  function schedulePreview() {
    window.clearTimeout(focusTimer);
    focusTimer = window.setTimeout(previewFocus, 400);
  }
  async function previewFocus() {
    const body = focusBody();
    const seq = ++focusSeq;
    markPreset();
    if (!body.text) { renderFocusPreview(null); return; }
    $("#focusPreview").classList.add("loading");
    try {
      const p = await post("/api/focus/preview", body);
      if (seq === focusSeq) renderFocusPreview(p);
    } catch (e) {
      if (seq === focusSeq) renderFocusPreview({ error: e.message });
    } finally {
      if (seq === focusSeq) $("#focusPreview").classList.remove("loading");
    }
  }
  function focusStat(value, label, note) {
    return h("div", { class: "fd-stat" }, h("b", { text: value }), h("span", { text: label }), note ? h("small", { text: note }) : null);
  }
  function renderFocusPreview(p) {
    const stats = $("#focusStats"), dist = $("#focusDist"), top = $("#focusTop"), note = $("#focusNote");
    top.replaceChildren();
    dist.hidden = true;
    $("#focusTopH").hidden = true;
    setText($("#focusMethod"), "");
    setText($("#focusKey"), "");
    setText($("#focusCalib"), "");
    if (!p) {
      stats.replaceChildren(focusStat("All news", "counts the same", "the standard, market-wide setting"));
      setText(note, "Pick an example or type the things you trade. The preview shows which recent headlines would count.");
      return;
    }
    if (p.error) {
      stats.replaceChildren();
      setText(note, p.error);
      return;
    }
    setText($("#focusMethod"), p.is_fallback ? "Matched by words (simplified: the AI model isn't loaded)" : "Matched by meaning (MiniLM AI vectors)");
    if (!p.effect) {
      stats.replaceChildren();
      setText(note, `Not enough news yet to learn what a typical headline looks like (${p.note || "calibrating"}). It switches on automatically once there is.`);
      return;
    }
    const e = p.effect;
    stats.replaceChildren(
      focusStat(pct(e.share, e.share < 0.1 ? 1 : 0), "of headlines match", `about ${e.per_hour.toFixed(e.per_hour < 10 ? 1 : 0)} an hour`),
      focusStat(`×${e.focus_multiplier.toFixed(1)}`, "pull of matching news", "on the score, vs. whole market"),
      focusStat(`×${e.other_multiplier.toFixed(2)}`, "pull of other news", "so big market-wide shocks still show"));
    const total = p.histogram.reduce((a, b) => a + b, 0) || 1;
    const names = ["Off topic", "Slightly", "Mostly", "Clearly on topic"];
    dist.replaceChildren(...p.histogram.map((n, i) => h("span", {
      class: `d${i}`, style: { "flex-grow": String(Math.max(n / total, 0.001)) }, title: `${names[i]}: ${n.toLocaleString("en-GB")} headlines`,
    }, n / total > 0.08 ? h("i", { text: names[i] }) : null)));
    dist.hidden = false;
    setText($("#focusKey"), p.histogram.map((n, i) => `${names[i]} ${pct(n / total, n / total < 0.1 ? 1 : 0)}`).join("  ·  "));
    $("#focusTopH").hidden = !p.top.length;
    top.replaceChildren(...p.top.map((t, i) => h("li", { style: { "--i": String(i) } },
      h("span", { class: "fd-rel" }, h("span", { style: { width: `${Math.max(4, t.relevance * 100)}%` } })),
      h("b", { text: pct(t.relevance) }),
      h("span", { class: "fd-hl", text: t.headline }),
      t.z == null ? null : h("span", { class: "x mono", text: `z ${t.z.toFixed(1)}` }))));
    const c = p.calibration;
    setText(note, (e.per_hour < 2
      ? "Few headlines match, so the focused score will be jumpier (it is still judged against its own normal). Adding related words catches more. "
      : "") + "Applying recalculates the last 7 days of “normal” with these weights, so the score compares like with like.");
    if (c) {
      setText($("#focusCalib"), `relevance r = logistic((z − ${c.z_mid}) / ${c.z_soft}), z = (cos − ${c.center.toFixed(3)}) / ${c.scale.toFixed(3)}`
        + ` (median / robust sd of ${c.headlines.toLocaleString("en-GB")} past headlines); weight w = ${p.background_weight} + ${(1 - p.background_weight).toFixed(2)}·r;`
        + ` effective share of weight ${pct(e.effective_share, 1)}`);
    }
  }
  async function applyFocus(text) {
    const btn = text ? $("#focusApply") : $("#focusReset");
    btn.classList.add("busy");
    try {
      const f = await post("/api/focus", { ...focusBody(), text });
      $("#focusDlg").close();
      toast(f.text ? "Now optimized for your focus" : "Back to the whole market",
        f.text ? `“${f.text}”. The last 7 days of “normal” were recalculated in ${f.recompute_seconds}s.` : "Every headline counts the same again.", "good", "target");
      S.history = [];
      await Promise.all([pollStatus(), pollHistory()]);
    } catch (e) {
      toast("Couldn't change the focus", e.message, "critical");
    } finally {
      btn.classList.remove("busy");
    }
  }
  function wireFocus() {
    $("#focusPresets").replaceChildren(...FOCUS_PRESETS.map((p) => h("button", {
      type: "button", class: "fd-chip", dataset: { text: p.text }, html: `${icon(p.icon)}<span>${p.label}</span>` })));
    $("#focusBtn").addEventListener("click", openFocus);
    $("#focusClose").addEventListener("click", () => $("#focusDlg").close());
    $("#focusDlg").addEventListener("click", (e) => { if (e.target === e.currentTarget) e.currentTarget.close(); });
    $("#focusPresets").addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      $("#focusText").value = b.dataset.text;
      previewFocus();
    });
    $("#focusText").addEventListener("input", schedulePreview);
    $("#focusText").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); applyFocus(focusBody().text); } });
    $("#focusBg").addEventListener("input", () => { setText($("#focusBgVal"), `${$("#focusBg").value}%`); schedulePreview(); });
    $("#focusApply").addEventListener("click", () => applyFocus(focusBody().text));
    $("#focusReset").addEventListener("click", () => applyFocus(""));
  }

  // ================================================================ bots tab
  class ECG {
    constructor(canvas) {
      this.c = canvas;
      this.ctx = canvas.getContext("2d");
      this.buf = [];
      this.pending = 0;
      this.wave = null;
      this.mode = "ok";
      this.running = false;
      this.acc = 0;
      this.tpl = ECG.template(50); // ~0.83 s per beat at 60 samples/s, so 1 Hz beats stay separate
      new ResizeObserver(() => this.resize()).observe(canvas.parentElement);
    }
    static template(n) {
      const out = [];
      for (let i = 0; i < n; i++) {
        const u = i / n;
        let v = 0;
        v += 0.12 * Math.exp(-Math.pow((u - 0.16) / 0.045, 2));
        v -= 0.12 * Math.exp(-Math.pow((u - 0.33) / 0.015, 2));
        v += 1.0 * Math.exp(-Math.pow((u - 0.38) / 0.018, 2));
        v -= 0.3 * Math.exp(-Math.pow((u - 0.43) / 0.018, 2));
        v += 0.24 * Math.exp(-Math.pow((u - 0.68) / 0.07, 2));
        out.push(v);
      }
      return out;
    }
    resize() {
      const r = this.c.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      this.W = r.width;
      this.H = r.height;
      this.c.width = Math.max(1, Math.round(r.width * dpr));
      this.c.height = Math.max(1, Math.round(r.height * dpr));
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const n = Math.max(2, Math.round(this.W / 2));
      while (this.buf.length < n) this.buf.unshift(0);
      while (this.buf.length > n) this.buf.shift();
    }
    beat(n) { this.pending = Math.min(3, this.pending + n); }
    start() {
      if (this.running) return;
      this.running = true;
      this.resize();
      this.last = performance.now();
      const loop = (t) => {
        if (!this.running) return;
        this.step(t);
        requestAnimationFrame(loop);
      };
      requestAnimationFrame(loop);
    }
    stop() { this.running = false; }
    step(t) {
      const dt = Math.min(0.1, (t - this.last) / 1000);
      this.last = t;
      this.acc += dt * 60; // 60 samples per second, 2 px each -> 120 px/s
      while (this.acc >= 1) {
        this.acc -= 1;
        let v = 0;
        if (this.wave != null) {
          v = this.tpl[this.wave++];
          if (this.wave >= this.tpl.length) this.wave = null;
        } else if (this.pending > 0 && this.mode !== "flat") {
          this.pending--;
          this.wave = 0;
        }
        if (this.mode !== "flat") v += (Math.random() - 0.5) * 0.02;
        this.buf.push(v);
        this.buf.shift();
      }
      this.draw();
    }
    draw() {
      const { ctx, W, H } = this;
      if (!W) return;
      ctx.clearRect(0, 0, W, H);
      ctx.strokeStyle = "rgba(74, 222, 128, 0.07)";
      ctx.lineWidth = 1;
      for (let x = 0; x < W; x += 20) { ctx.beginPath(); ctx.moveTo(x + 0.5, 0); ctx.lineTo(x + 0.5, H); ctx.stroke(); }
      for (let y = 0; y < H; y += 20) { ctx.beginPath(); ctx.moveTo(0, y + 0.5); ctx.lineTo(W, y + 0.5); ctx.stroke(); }
      const color = this.mode === "flat" ? "#f87171" : this.mode === "warn" ? "#fcd34d" : "#4ade80";
      const base = H * 0.62, amp = H * 0.46;
      ctx.save();
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.shadowColor = color;
      ctx.shadowBlur = 10;
      ctx.beginPath();
      this.buf.forEach((v, i) => { const x = (i / (this.buf.length - 1)) * W, y = base - v * amp; if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
      ctx.stroke();
      const lastY = base - this.buf[this.buf.length - 1] * amp;
      ctx.fillStyle = color;
      ctx.beginPath(); ctx.arc(W - 3, lastY, 3.5, 0, Math.PI * 2); ctx.fill();
      ctx.restore();
      const g = ctx.createLinearGradient(0, 0, W * 0.25, 0);
      g.addColorStop(0, "rgba(5, 11, 16, 0.95)");
      g.addColorStop(1, "rgba(5, 11, 16, 0)");
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, W * 0.25, H);
    }
  }
  const ecg = new ECG($("#ecg"));

  function renderBots(s) {
    const c = s.client;
    const badge = $("#ecgBadge"), face = $("#botFace");
    if (!c) {
      ecg.mode = "flat";
      setText(badge, "No example bot running");
      badge.className = "ecg-badge bad";
      setText($("#botTitle"), "No example bot");
      setText($("#botDesc"), "Start the dashboard with its built-in demo client to see a bot here.");
      return;
    }
    if (S.lastValid != null && c.stats.valid > S.lastValid) ecg.beat(c.stats.valid - S.lastValid);
    S.lastValid = c.stats.valid;
    const stale = c.heartbeat_age_s == null || c.heartbeat_age_s > c.timeout_s || c.fault === "stale_heartbeat";
    ecg.mode = stale ? "flat" : c.in_fault ? "warn" : "ok";
    const L = level(c.local_state);
    let title, desc, tone;
    if (stale) {
      title = "Stopped itself — no heartbeat";
      desc = `It hasn't heard from Veto Sentinel for ${c.heartbeat_age_s == null ? "a while" : `${c.heartbeat_age_s.toFixed(1)} s`}, so it blocks every order until two good heartbeats arrive.`;
      tone = "bad";
      setText(badge, "NO HEARTBEAT · bot stopped itself");
      badge.className = "ecg-badge bad";
    } else if (c.in_fault) {
      const f = FAULTS[c.fault] || ["Paused", ""];
      title = `Paused for safety: ${f[0].toLowerCase()}`;
      desc = f[1];
      tone = "warn";
      setText(badge, `Heartbeat OK · bot paused (${f[0].toLowerCase()})`);
      badge.className = "ecg-badge warn";
    } else {
      title = c.local_state === "NORMAL" ? "Trading normally" : c.local_state === "CAUTION" ? "Trading carefully" : c.local_state === "FLATTEN" ? "Emergency — waiting for a human" : "Stopped — Veto Sentinel says stop";
      desc = c.local_state === "NORMAL" ? "It hears a heartbeat every second and Veto Sentinel says all clear."
        : c.local_state === "CAUTION" ? `It only reduces positions, at most ${Math.round((s.caution_position_fraction || 0.25) * 100)}% of the usual size.`
          : L.desc;
      tone = c.local_state === "NORMAL" ? "ok" : c.local_state === "CAUTION" ? "warn" : "bad";
      setText(badge, `Heartbeat OK · following “${L.title}”`);
      badge.className = "ecg-badge";
    }
    if (face.dataset.tone !== tone) face.dataset.tone = tone;
    setText($("#botTitle"), title);
    setText($("#botDesc"), desc);
    renderPerms($("#botPerms"), stale || c.in_fault ? "HALT" : c.local_state, s.caution_position_fraction || 0.25);
    setText($("#hbAge"), c.heartbeat_age_s == null ? "never" : `${c.heartbeat_age_s.toFixed(1)} s ago`);
    setText($("#hbCount"), c.stats.received.toLocaleString("en-GB"));
    setText($("#hbRecov"), String(c.stats.recoveries));
    setText($("#hbSeq"), c.last_seq == null ? "–" : `${c.last_seq} · ${c.publisher_id || ""}`);
    // fault tiles
    const box = $("#faultTiles");
    const entries = Object.entries(c.stats.faults).filter(([, n]) => n > 0);
    if (!entries.length) {
      if (!box.querySelector(".empty")) box.replaceChildren(h("p", { class: "empty", text: "Nothing yet. Try one of the buttons above." }));
    } else {
      for (const [k, n] of entries) {
        let t = box.querySelector(`[data-fault="${k}"]`);
        if (!t) {
          box.querySelector(".empty")?.remove();
          t = h("div", { class: "tile", dataset: { fault: k } }, h("div", { class: "t-label", text: (FAULTS[k] || [k])[0] }), h("div", { class: "t-value", text: "0" }), h("div", { class: "t-note", text: (FAULTS[k] || ["", ""])[1] }));
          box.append(t);
        }
        const v = $(".t-value", t);
        if (v.textContent !== String(n)) {
          setText(v, String(n));
          if (S.faultCounts[k] != null) { t.classList.remove("bump"); void t.offsetWidth; t.classList.add("bump"); }
          S.faultCounts[k] = n;
        }
      }
    }
  }

  // ================================================================ controls tab
  const SPEEDS = [600, 1800, 3600, 10800, 43200];
  function renderControls(s) {
    const r = s.replay;
    renderFlatten(s);
    const live = r.mode !== "replay";
    $("#playerCard").classList.toggle("is-live", live);
    $("#playerCard").hidden = live; // live news can't be paused or fast-forwarded
    $("#eventsCard").hidden = live;
    if (live) return;
    setText($("#replaySub"), `${r.synthetic ? "Invented demo news" : r.data_origin.replace(/_/g, " ")} · ${r.fixture_title || r.fixture}`);
    setText($("#simTime"), fmtFull(r.sim_time));
    setText($("#replayStatus"), r.finished ? "Finished" : r.playing ? "Playing" : "Paused");
    const pb = $("#playBtn");
    const playing = r.playing && !r.finished;
    if (pb.dataset.playing !== String(playing)) {
      pb.dataset.playing = String(playing);
      pb.innerHTML = icon(playing ? "pause" : "play");
      pb.setAttribute("aria-label", playing ? "Pause" : "Play");
      pb.classList.toggle("playing", playing);
    }
    $("#playBtn").disabled = r.finished;
    $("#stepBtn").disabled = r.finished || playing;
    const pct = `${(clamp(r.progress, 0, 1) * 100).toFixed(2)}%`;
    $("#scrubFill").style.width = pct;
    $("#scrubHead").style.left = pct;
    setText($("#scrubStart"), fmtDay(r.start));
    setText($("#scrubEnd"), fmtDay(r.end));
    for (const b of $$("#speedSeg button")) b.classList.toggle("on", Number(b.dataset.speed) === r.speed);
    // markers + events (rebuilt when the run changes)
    const key = `${s.run_id}|${(r.scenarios || []).map((x) => x.name).join(",")}`;
    const start = Date.parse(r.start), end = Date.parse(r.end), now = Date.parse(r.sim_time);
    if (key !== S.markersKey) {
      S.markersKey = key;
      $$(".scrub-marker").forEach((m) => m.remove());
      for (const sc of r.scenarios || []) {
        const pos = (Date.parse(sc.onset) - start) / (end - start);
        if (!(pos >= 0 && pos <= 1)) continue;
        $("#scrubber").append(h("button", { type: "button", class: "scrub-marker", style: { left: `${(pos * 100).toFixed(2)}%` },
          dataset: { label: scenarioCopy(sc).title, onset: sc.onset }, "aria-label": `Jump to ${scenarioCopy(sc).title}` }));
      }
      $("#events").replaceChildren(...(r.scenarios || []).map((sc) => {
        const copy = scenarioCopy(sc);
        return h("button", { type: "button", class: "event", dataset: { onset: sc.onset, end: sc.end, title: copy.title } },
          h("span", { html: icon(copy.icon) }), h("b", { text: copy.title }), h("small", { text: copy.text }),
          h("span", { class: "when", text: fmtFull(sc.onset) }));
      }));
      if (!(r.scenarios || []).length) $("#events").replaceChildren(h("p", { class: "empty", text: "This replay has no labelled events." }));
    }
    for (const ev of $$("#events .event")) {
      const onset = Date.parse(ev.dataset.onset), evEnd = Date.parse(ev.dataset.end);
      const passed = now >= evEnd, during = now >= onset && now < evEnd, tooLate = now > onset - 2 * 3600e3;
      ev.classList.toggle("passed", passed);
      ev.disabled = tooLate;
      const when = $(".when", ev);
      setText(when, during ? `Happening now (since ${fmtClock(ev.dataset.onset)})` : fmtFull(ev.dataset.onset));
    }
  }
  function renderFlatten(s) {
    const active = s.state === "FLATTEN" && s.flatten;
    $("#flatActive").hidden = !active;
    $("#fsteps").style.display = active ? "none" : "";
    if (active) setText($("#flatActiveText"), `Emergency instruction active — issued by ${s.flatten.operator}: “${s.flatten.reason}”. Bots are blocked; a person must close positions.`);
  }

  async function seekTo(onsetIso, title, btn) {
    const target = new Date(Date.parse(onsetIso) - 2 * 3600e3).toISOString();
    if (btn) btn.classList.add("busy");
    toast("Fast-forwarding…", `Skipping ahead to 2 hours before “${title}”. With the AI models this can take a little while.`, "info", "forward");
    try {
      const r = await post("/api/replay-seek", { to: target });
      toast("Arrived", `It's now ${fmtFull(r.sim_time)} in the replay. Press play and watch the overview.`, "good", "check");
    } catch (e) {
      toast("Couldn't jump", e.message, "critical", "alert");
    } finally {
      if (btn) btn.classList.remove("busy");
      pollStatus();
    }
  }
  function findCrisis(s) {
    const scs = (s.replay.scenarios || []).filter((x) => x.kind === "stress");
    return scs.find((x) => x.name === "bank_stress") || scs[scs.length - 1] || null;
  }

  // ================================================================ how-it-works tab
  function buildHow() {
    const pipe = $("#pipeline");
    PIPE.forEach((p, i) => {
      if (i) pipe.append(h("span", { class: "pipe-link", "aria-hidden": "true" }));
      pipe.append(h("button", { type: "button", class: "pipe-step", "aria-pressed": String(i === 0), dataset: { i: String(i) }, style: { "--i": String(i) } },
        h("span", { class: "p-ico", html: icon(p.icon) }), h("b", { text: p.t }), h("small", { text: p.s })));
    });
    showPipe(0);
    pipe.addEventListener("click", (e) => {
      const b = e.target.closest(".pipe-step");
      if (!b) return;
      showPipe(Number(b.dataset.i));
    });
    $("#levels").replaceChildren(...["NORMAL", "CAUTION", "HALT", "FLATTEN"].map((st) => {
      const L = level(st);
      const does = { NORMAL: "Bots: trade normally.", CAUTION: "Bots: only reduce positions, smaller size.", HALT: "Bots: no new orders; cancel waiting orders.", FLATTEN: "Bots: no orders. A person closes positions." }[st];
      return h("div", { class: "level", dataset: { state: st }, style: { "--lc": `var(--${L.tone})`, "--lon": L.tone === "warning" ? "#0b0b0b" : "#fff" } },
        h("div", { class: "lvl-head" }, h("span", { class: "lvl-badge", html: icon(L.icon) }), h("div", null, h("small", { text: L.code }), h("b", { text: L.title }))),
        h("p", { text: L.desc }), h("div", { class: "bots-do", text: does }));
    }));
    $("#glossary").replaceChildren(...GLOSSARY.map(([term, text], i) => {
      const id = `gl-${i}`;
      const btn = h("button", { type: "button", "aria-expanded": "false", "aria-controls": id }, h("span", { text: term }), h("span", { html: icon("chevron") }));
      const item = h("div", { class: "gl-item" }, btn, h("div", { class: "gl-body", id }, h("div", null, h("p", { text }))));
      btn.addEventListener("click", () => {
        const open = !item.classList.contains("open");
        item.classList.toggle("open", open);
        btn.setAttribute("aria-expanded", String(open));
      });
      return item;
    }));
  }
  function showPipe(i) {
    S.pipeStep = i;
    for (const b of $$(".pipe-step")) b.setAttribute("aria-pressed", String(Number(b.dataset.i) === i));
    const d = $("#pipeDetail");
    setText($("h3", d), PIPE[i].title);
    setText($("p", d), PIPE[i].text);
    d.classList.remove("swap");
    void d.offsetWidth;
    d.classList.add("swap");
  }
  function renderHow(s) {
    for (const el of $$("#levels .level")) el.classList.toggle("current", el.dataset.state === s.state);
    const impl = (k) => {
      const v = s.implementations[k];
      return v ? `${v.implementation}${v.is_fallback ? " · simplified fallback" : ""}` : "–";
    };
    const th = s.thresholds, p = s.publisher;
    const kv = [
      ["Mode", s.mode === "replay" ? "Replay" : "Live"],
      ["Data", `${s.data_origin.replace(/_/g, " ")}${s.synthetic ? " (invented)" : ""}`],
      ["Negativity model", impl("panic")],
      ["Topic model", impl("drift")],
      ["Volume method", impl("volume")],
      ["Thresholds", `Be careful ≥ ${th.caution_enter} (relax below ${th.caution_exit}) · Stop ≥ ${th.halt_enter} (relax below ${th.halt_exit}) · cool-off ${th.cooldown_minutes} min`],
      ["Weights", `negativity ${s.weights.panic} · topic change ${s.weights.drift} · news volume ${s.weights.volume}`],
      ["Parameters", s.parameter_status],
      ["Message bus", p ? `${p.endpoint} · heartbeat every ${p.heartbeat_interval_ms} ms · bots time out after ${p.heartbeat_timeout_ms} ms · message #${p.seq}` : "not running"],
      ["Counters", Object.entries(s.counters).map(([k, v]) => `${k.replace(/_/g, " ")} ${v}`).join(" · ")],
      ["Run", s.run_id],
    ];
    const key = JSON.stringify(kv);
    const dl = $("#hood");
    if (dl.dataset.key !== key) {
      dl.dataset.key = key;
      dl.replaceChildren(...kv.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: v })]));
    }
    const sk = JSON.stringify(s.adapters);
    const tb = $("#sources");
    if (tb.dataset.key !== sk) {
      tb.dataset.key = sk;
      tb.replaceChildren(...s.adapters.map((a) => h("tr", null, h("td", { text: `${a.name} (${a.data_origin.replace(/_/g, " ")})` }),
        h("td", { text: a.last_success ? fmtFull(a.last_success) : "never" }), h("td", { class: "num", text: String(a.total_items) }),
        h("td", { class: "err", text: a.last_error || "" }))));
    }
  }

  // ================================================================ status handler
  function onStatus(s) {
    if (s.run_id !== S.runId) {
      if (S.runId) toast("Replay restarted", "Starting again from day 1. Veto Sentinel warms up first, so it begins at “Stop”.", "info", "restart");
      S.runId = s.run_id;
      S.history = [];
      S.transitions = [];
      S.transKey = "";
      S.markersKey = "";
      S.heroState = null;
      S.prevState = null;
      chartsAnimated = { overview: false, signals: false, timeline: false };
      pollHistory();
      pollTransitions();
    }
    S.status = s;
    document.body.dataset.state = s.state;
    if (S.prevState && S.prevState !== s.state) {
      const from = level(S.prevState), to = level(s.state), now = performance.now();
      if (now - S.lastStateToast > 3500) {
        S.lastStateToast = now;
        toast(`Now: ${to.title}`, `Changed from “${from.title}”. ${to.desc}`, to.tone, to.icon);
      }
      const hero = $("#heroCard");
      hero.classList.remove("flash");
      void hero.offsetWidth;
      hero.classList.add("flash");
    }
    S.prevState = s.state;
    renderTop(s);
    renderNotices(s);
    renderHero(s);
    renderGauge(s);
    const p = renderPerms($("#perms"), s.state, s.caution_position_fraction || 0.25);
    setText($("#sizeText"), `${Math.round(p.size * 100)}%`);
    $("#sizeBar").style.width = `${p.size * 100}%`;
    renderMiniSignals(s);
    renderSignals(s);
    renderNews(s);
    renderFocusBtn(s);
    renderBots(s);
    renderControls(s);
    renderHow(s);
    renderMiniLegend();
    setText($("#miniSub"), s.mode === "replay" ? "simulated time" : "UTC");
  }

  // ================================================================ polling
  const inflight = { status: false, history: false, transitions: false };
  async function pollStatus() {
    if (inflight.status) return;
    inflight.status = true;
    try {
      const s = await api("/api/status");
      if (S.offline) { S.offline = false; toast("Reconnected", "Live data is flowing again.", "good", "check"); }
      onStatus(s);
    } catch (e) {
      if (e.message !== "login required" && !S.offline) {
        S.offline = true;
        if (S.status) renderNotices(S.status); else $("#noticeOffline").hidden = false;
      }
    } finally {
      inflight.status = false;
    }
  }
  async function pollHistory() {
    if (inflight.history || !S.runId) return;
    inflight.history = true;
    const run = S.runId;
    try {
      const last = S.history.length ? S.history[S.history.length - 1].as_of : null;
      const rows = await api(last ? `/api/history?limit=5000&since=${encodeURIComponent(last)}` : "/api/history?limit=6000");
      if (run !== S.runId) return;
      if (rows.length) {
        S.history.push(...rows);
        if (S.history.length > 20000) S.history.splice(0, S.history.length - 20000);
        renderCharts(false);
      }
    } catch (_) { /* reported by the status poll */ } finally {
      inflight.history = false;
    }
  }
  async function pollTransitions() {
    if (inflight.transitions || !S.runId) return;
    inflight.transitions = true;
    const run = S.runId;
    try {
      const rows = await api("/api/transitions?limit=100");
      if (run === S.runId) { S.transitions = rows; renderChanges(); }
    } catch (_) { /* reported by the status poll */ } finally {
      inflight.transitions = false;
    }
  }

  // ================================================================ events
  function ripple(e, el) {
    if (reduceMotion) return;
    const r = el.getBoundingClientRect();
    const d = Math.max(r.width, r.height) * 0.5;
    const s = h("span", { class: "ripple-fx", style: { width: `${d}px`, height: `${d}px`, left: `${e.clientX - r.left - d / 2}px`, top: `${e.clientY - r.top - d / 2}px` } });
    el.append(s);
    window.setTimeout(() => s.remove(), 750);
  }

  function wire() {
    hydrateIcons();
    // tabs
    $("#tabs").addEventListener("click", (e) => {
      const b = e.target.closest("[role=tab]");
      if (b) selectTab(b.dataset.tab);
    });
    $("#tabs").addEventListener("keydown", (e) => {
      const i = TABS.indexOf(S.tab);
      let n = null;
      if (e.key === "ArrowRight") n = TABS[(i + 1) % TABS.length];
      else if (e.key === "ArrowLeft") n = TABS[(i - 1 + TABS.length) % TABS.length];
      else if (e.key === "Home") n = TABS[0];
      else if (e.key === "End") n = TABS[TABS.length - 1];
      if (n) { e.preventDefault(); selectTab(n, { focus: true }); }
    });
    window.addEventListener("resize", placeIndicator);
    $("#zoomReset")?.addEventListener("click", () => bigChart.resetZoom());
    wireFocus();

    document.addEventListener("click", (e) => {
      const help = e.target.closest(".help");
      if (help) { openPopover(help); return; }
      if (!e.target.closest("#popover")) closePopover();
      const go = e.target.closest("[data-goto]");
      if (go) { selectTab(go.dataset.goto); window.scrollTo({ top: 0, behavior: reduceMotion ? "auto" : "smooth" }); }
      const fx = e.target.closest(".qa, .fault, .event");
      if (fx) ripple(e, fx);
    });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closePopover(); hideTips(); } });
    window.addEventListener("scroll", () => { if (!$("#chartTip").hidden) hideTips(); }, { passive: true });
    window.addEventListener("blur", hideTips);

    // card spotlight
    document.addEventListener("pointermove", (e) => {
      const card = e.target.closest && e.target.closest(".card");
      if (!card) return;
      const r = card.getBoundingClientRect();
      card.style.setProperty("--x", `${e.clientX - r.left}px`);
      card.style.setProperty("--y", `${e.clientY - r.top}px`);
    }, { passive: true });

    // expert + theme
    const root = document.documentElement;
    const expert = $("#expertToggle");
    expert.checked = root.classList.contains("expert");
    expert.addEventListener("change", () => {
      root.classList.toggle("expert", expert.checked);
      try { localStorage.setItem("vs-expert", expert.checked ? "1" : "0"); } catch (_) { /* ignore */ }
    });
    const themeBtn = $("#themeToggle");
    const current = () => root.dataset.theme || (window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
    const paintThemeBtn = () => { themeBtn.innerHTML = icon(current() === "dark" ? "sun" : "moon"); };
    paintThemeBtn();
    themeBtn.addEventListener("click", () => {
      const next = current() === "dark" ? "light" : "dark";
      root.dataset.theme = next;
      try { localStorage.setItem("vs-theme", next); } catch (_) { /* ignore */ }
      paintThemeBtn();
      renderCharts(false);
    });

    // logout
    $("#logoutBtn").addEventListener("click", async () => {
      try { await post("/api/logout"); } catch (_) { /* go anyway */ }
      document.body.style.transition = "opacity .4s";
      document.body.style.opacity = "0";
      window.setTimeout(() => location.assign("/login"), 380);
    });

    // timeline filters
    $("#rangeSeg").addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      S.range = Number(b.dataset.range);
      for (const x of $$("#rangeSeg button")) x.classList.toggle("on", x === b);
      chartsAnimated.timeline = false;
      bigChart.view = null; // a new range starts zoomed out
      renderCharts(false);
    });
    $("#seriesToggles").addEventListener("change", (e) => {
      const k = e.target.dataset.series;
      if (!k) return;
      S.series[k] = e.target.checked;
      renderCharts(false);
    });
    $("#tableToggle").addEventListener("click", () => {
      S.table = !S.table;
      $("#tableToggle").setAttribute("aria-pressed", String(S.table));
      setText($("#tableToggle"), S.table ? "Hide table" : "Show as table");
      $("#tableView").hidden = !S.table;
      renderCharts(false);
    });

    // quick actions
    $(".quick").addEventListener("click", async (e) => {
      const b = e.target.closest("[data-action]");
      if (!b || !S.status) return;
      const a = b.dataset.action;
      if (a === "play") {
        if (S.status.replay.mode !== "replay") { toast("Live mode", "There's nothing to replay in live mode.", "info"); return; }
        try { await post("/api/replay/play"); toast("Replay playing", "Days of news now pass in minutes. Watch the level and the gauge.", "good", "play"); } catch (err) { toast("Couldn't start", err.message, "critical"); }
        pollStatus();
      } else if (a === "jump-stress") {
        const sc = findCrisis(S.status);
        if (!sc) { toast("No crisis in this replay", "This data has no labelled stress event.", "info"); return; }
        const now = Date.parse(S.status.replay.sim_time), onset = Date.parse(sc.onset), end = Date.parse(sc.end);
        if (now >= end) { toast("Already passed", "Restart the replay (Controls tab) to watch it again.", "warning", "restart"); return; }
        if (now > onset - 2 * 3600e3) { toast("It's happening now", "Keep watching the overview.", "info", "alert"); return; }
        await seekTo(sc.onset, scenarioCopy(sc).title, b);
        try { await post("/api/replay/play"); } catch (_) { /* ignore */ }
      } else if (a === "break") {
        selectTab("bots");
        await injectFault("silence_5s");
      }
    });

    // fault injection
    $(".faults").addEventListener("click", (e) => {
      const b = e.target.closest("[data-fault]");
      if (b) injectFault(b.dataset.fault, b);
    });

    // replay controls
    $("#playBtn").addEventListener("click", async () => {
      const playing = $("#playBtn").dataset.playing === "true";
      try { await post(`/api/replay/${playing ? "pause" : "play"}`); } catch (err) { toast("Couldn't change playback", err.message, "critical"); }
      pollStatus();
    });
    $("#stepBtn").addEventListener("click", async () => {
      try { await post("/api/replay/step"); } catch (err) { toast("Couldn't step", err.message, "critical"); }
      pollStatus();
    });
    let restartTimer = 0;
    $("#restartBtn").addEventListener("click", async () => {
      const btn = $("#restartBtn");
      if (!btn.classList.contains("confirm")) {
        btn.classList.add("confirm");
        setText($(".lbl", btn), "Click again to restart");
        restartTimer = window.setTimeout(() => { btn.classList.remove("confirm"); setText($(".lbl", btn), "Restart"); }, 3500);
        return;
      }
      window.clearTimeout(restartTimer);
      btn.classList.remove("confirm");
      setText($(".lbl", btn), "Restart");
      try { await post("/api/replay/restart"); } catch (err) { toast("Couldn't restart", err.message, "critical"); }
      pollStatus();
    });
    $("#speedSeg").addEventListener("click", async (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      try { await post("/api/replay-speed", { speed: Number(b.dataset.speed) }); } catch (err) { toast("Couldn't change speed", err.message, "critical"); }
      pollStatus();
    });
    $("#scrubber").addEventListener("click", (e) => {
      const m = e.target.closest(".scrub-marker");
      if (!m || !S.status) return;
      const now = Date.parse(S.status.replay.sim_time);
      if (now > Date.parse(m.dataset.onset) - 2 * 3600e3) { toast("Can't go back in time", "The replay only moves forward. Restart it to watch this again.", "warning", "restart"); return; }
      seekTo(m.dataset.onset, m.dataset.label, null);
    });
    $("#events").addEventListener("click", (e) => {
      const ev = e.target.closest(".event");
      if (ev && !ev.disabled) seekTo(ev.dataset.onset, ev.dataset.title, ev);
    });

    // FLATTEN
    let codeTimer = 0;
    const step2 = $("#fstep2"), conf = $("#flatConf"), msg = $("#opMsg");
    const setMsg = (text, err) => { msg.classList.toggle("err", !!err); msg.replaceChildren(text); };
    function lockStep2() {
      step2.setAttribute("aria-disabled", "true");
      step2.classList.remove("ready");
      conf.disabled = true;
      $("#opCode").value = "";
      setText($("#codeTimer"), "");
      window.clearInterval(codeTimer);
    }
    $("#flatReq").addEventListener("click", async () => {
      const operator = $("#opName").value.trim(), reason = $("#opReason").value.trim();
      if (!operator || !reason) { setMsg("Enter your name and a reason first.", true); return; }
      try {
        const r = await post("/api/operator/flatten/request", { operator, reason });
        $("#opCode").value = r.confirmation_code;
        step2.setAttribute("aria-disabled", "false");
        step2.classList.add("ready");
        conf.disabled = false;
        msg.classList.remove("err");
        msg.replaceChildren("Step 2: confirm with code ", h("span", { class: "code", text: r.confirmation_code }), " — it expires in 2 minutes. Nothing has been sent yet.");
        const expires = Date.parse(r.expires);
        window.clearInterval(codeTimer);
        codeTimer = window.setInterval(() => {
          const left = Math.max(0, Math.round((expires - Date.now()) / 1000));
          setText($("#codeTimer"), left ? `expires in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}` : "code expired — request again");
          if (!left) { window.clearInterval(codeTimer); conf.disabled = true; step2.classList.remove("ready"); }
        }, 500);
      } catch (err) { setMsg(`Request failed: ${err.message}`, true); }
    });
    conf.addEventListener("click", async () => {
      const operator = $("#opName").value.trim(), code = $("#opCode").value.trim();
      try {
        await post("/api/operator/flatten/confirm", { operator, code });
        lockStep2();
        setMsg("Emergency instruction broadcast. Bots are blocked and a person must close positions.");
        toast("Emergency instruction sent", "All connected bots now show “Emergency”. Veto Sentinel does not trade.", "critical", "siren");
        pollStatus();
      } catch (err) { setMsg(`Not confirmed: ${err.message}`, true); }
    });
    $("#flatClear").addEventListener("click", async () => {
      const operator = $("#opName").value.trim() || S.user || "operator";
      try {
        await post("/api/operator/flatten/clear", { operator, reason: "cleared from the dashboard" });
        setMsg("Emergency cancelled. Veto Sentinel resumes at “Stop” and relaxes step by step.");
        toast("Emergency cancelled", "Resuming at “Stop”; it relaxes again once things are calm.", "warning", "restart");
        pollStatus();
      } catch (err) { setMsg(`Couldn't cancel: ${err.message}`, true); }
    });
  }

  async function injectFault(kind, btn) {
    const names = { silence_5s: "Heartbeat cut for 5 seconds", malformed: "Corrupted message sent", seq_gap: "Messages made to go missing" };
    const hints = { silence_5s: "Watch the monitor flatline and the bot stop itself after ~3 s.", malformed: "The bot rejects it and pauses until two good heartbeats arrive.", seq_gap: "The bot spots the gap and pauses briefly." };
    if (btn) btn.classList.add("busy");
    try {
      await post(`/api/fault/${kind}`);
      toast(names[kind] || kind, hints[kind] || "", "warning", "bolt");
    } catch (err) {
      toast("Couldn't inject the fault", err.message, "critical");
    } finally {
      if (btn) window.setTimeout(() => btn.classList.remove("busy"), 600);
    }
  }

  // ================================================================ start
  async function start() {
    wire();
    buildSignalCards();
    buildHow();
    $$(".grid").forEach((g) => Array.from(g.children).forEach((c, i) => c.style.setProperty("--i", String(i))));
    const fromHash = location.hash.replace("#", "");
    selectTab(TABS.includes(fromHash) ? fromHash : "overview");
    try {
      const sess = await api("/api/session");
      S.user = sess.user;
      setText($("#userName"), sess.user || "");
      setText($("#avatar"), (sess.user || "?").charAt(0).toUpperCase());
      if (!$("#opName").value) $("#opName").value = sess.user || "";
    } catch (_) { /* status poll handles redirects */ }
    await pollStatus();
    await Promise.all([pollHistory(), pollTransitions()]);
    renderCharts(true);
    window.setInterval(pollStatus, 1000);
    window.setInterval(pollHistory, 2000);
    window.setInterval(pollTransitions, 3000);
    window.setTimeout(placeIndicator, 300);
  }
  start();
})();
