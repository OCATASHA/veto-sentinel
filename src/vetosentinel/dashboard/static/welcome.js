/* Veto Sentinel welcome screen + login.
 *
 * The canvas scene is purely decorative: a slowly scrolling candlestick tape (markets), market
 * "dust", and finance words streaming into the shield (Veto Sentinel reading the news). Each time
 * the shield's CSS heartbeat pulses, the canvas emits a matching ring (the 1 Hz heartbeat that
 * trading bots listen to). Nothing on this page is real data.
 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const welcome = $("welcome");
  const login = $("login");
  const form = $("login-form");
  const errorEl = $("login-error");
  const curtain = $("curtain");
  let current = location.pathname === "/login" ? "login" : "welcome";

  // ------------------------------------------------------------------ title letters
  const title = $("title");
  const text = title.dataset.text || "Veto Sentinel";
  title.textContent = "";
  title.classList.add("title-glow");
  [...text].forEach((ch, i) => {
    const span = document.createElement("span");
    span.className = "letter" + (i >= 5 ? " accent" : "");
    span.style.setProperty("--i", String(i));
    span.setAttribute("aria-hidden", "true");
    span.textContent = ch === " " ? "\u00a0" : ch; // a lone space would collapse inside inline-block
    title.appendChild(span);
  });

  // ------------------------------------------------------------------ scene
  const scene = createScene($("scene"));

  // Sync canvas heartbeat rings with the CSS pulse on the shield.
  document.querySelector(".emblem .beat").addEventListener("animationiteration", () => {
    if (current === "welcome") scene.pulse();
  });

  // Gentle parallax.
  if (!reduceMotion) {
    window.addEventListener("pointermove", (e) => {
      const x = e.clientX / window.innerWidth - 0.5;
      const y = e.clientY / window.innerHeight - 0.5;
      document.documentElement.style.setProperty("--mx", x.toFixed(3));
      document.documentElement.style.setProperty("--my", y.toFixed(3));
      scene.parallax(x, y);
    }, { passive: true });
  }

  // ------------------------------------------------------------------ screens
  function attractor() {
    const el = current === "login" ? document.querySelector(".card-emblem") : document.querySelector(".emblem");
    return el;
  }

  function show(name) {
    if (name === current && !(name === "welcome" ? welcome.hidden : login.hidden)) return;
    const from = name === "login" ? welcome : login;
    const to = name === "login" ? login : welcome;
    current = name;
    history.replaceState(null, "", name === "login" ? "/login" : "/");
    from.classList.add("is-leaving");
    window.setTimeout(() => {
      from.hidden = true;
      from.classList.remove("is-leaving", "is-active");
      to.hidden = false;
      if (name === "welcome") {
        welcome.classList.remove("play");
        void welcome.offsetWidth; // restart the intro choreography
        welcome.classList.add("play");
      } else {
        login.classList.add("is-active");
        window.setTimeout(() => $("username").focus({ preventScroll: true }), reduceMotion ? 0 : 350);
      }
      requestAnimationFrame(() => scene.setTarget(attractor()));
    }, reduceMotion ? 0 : 430);
  }

  async function advance() {
    if (current !== "welcome") return;
    try {
      const r = await fetch("/api/session", { cache: "no-store", credentials: "same-origin" });
      if (r.ok && (await r.json()).authenticated) {
        goDashboard();
        return;
      }
    } catch (_) { /* server unreachable: the login form will report it */ }
    show("login");
  }

  function goDashboard() {
    curtain.classList.add("on");
    window.setTimeout(() => location.assign("/dashboard"), reduceMotion ? 0 : 600);
  }

  $("enter").addEventListener("click", advance);
  document.querySelector(".enter-fill").addEventListener("animationend", (e) => {
    if (e.animationName === "autoFill" && !reduceMotion) advance();
  });
  document.addEventListener("keydown", (e) => {
    if (current === "welcome" && (e.key === "Enter" || e.key === " ") && document.activeElement === document.body) {
      e.preventDefault();
      advance();
    }
  });
  $("back").addEventListener("click", () => show("welcome"));

  // ------------------------------------------------------------------ login
  let busy = false;
  $("reveal").addEventListener("click", () => {
    const pw = $("password");
    const showing = pw.type === "text";
    pw.type = showing ? "password" : "text";
    $("reveal").setAttribute("aria-pressed", String(!showing));
    $("reveal").setAttribute("aria-label", showing ? "Show password" : "Hide password");
    pw.focus();
  });

  function fail(message) {
    errorEl.textContent = message;
    form.classList.remove("shake");
    void form.offsetWidth;
    form.classList.add("shake");
    scene.reject();
    const pw = $("password");
    pw.focus();
    pw.select();
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (busy) return;
    const username = $("username").value.trim();
    const password = $("password").value;
    if (!username || !password) {
      fail("Please enter your username and password.");
      return;
    }
    busy = true;
    errorEl.textContent = "";
    form.classList.add("busy");
    try {
      const r = await fetch("/api/login", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      if (r.ok) {
        form.classList.remove("busy");
        form.classList.add("success");
        scene.celebrate();
        window.setTimeout(goDashboard, reduceMotion ? 0 : 1150);
        return;
      }
      let message = "Wrong username or password.";
      try {
        const body = await r.json();
        if (typeof body.detail === "string") message = body.detail;
      } catch (_) { /* keep default */ }
      if (r.status === 422) message = "Please enter your username and password.";
      fail(message);
    } catch (_) {
      fail("Can't reach the Veto Sentinel server. Is it still running?");
    } finally {
      busy = false;
      form.classList.remove("busy");
    }
  });

  // ------------------------------------------------------------------ start
  if (current === "login") {
    welcome.hidden = true;
    welcome.classList.remove("play");
    login.hidden = false;
    login.classList.add("is-active");
    window.setTimeout(() => $("username").focus({ preventScroll: true }), 300);
  }
  requestAnimationFrame(() => scene.setTarget(attractor()));
  window.addEventListener("resize", () => scene.setTarget(attractor()));

  // ================================================================== scene
  function createScene(canvas) {
    const ctx = canvas.getContext("2d");
    const WORDS = [
      ["INFLATION", 0], ["RATES", 0], ["BANKS", 0], ["LIQUIDITY", 0], ["YIELDS", 0], ["CREDIT", 0],
      ["EARNINGS", 0], ["CENTRAL BANK", 0], ["BONDS", 0], ["GDP", 0], ["JOBS", 0], ["CURRENCIES", 0],
      ["DEPOSITS", 0], ["OIL", 0], ["TARIFFS", 0], ["SPREADS", 0], ["GUIDANCE", 0], ["OUTLOOK", 0],
      ["RALLY", 1], ["RECOVERY", 1], ["CALM", 1], ["GROWTH", 1], ["UPGRADE", 1],
      ["DEFAULT", -1], ["RECESSION", -1], ["SELL-OFF", -1], ["PANIC", -1], ["BANK RUN", -1],
      ["VOLATILITY", -1], ["CONTAGION", -1], ["DOWNGRADE", -1], ["RISK-OFF", -1],
    ];
    const TONE = { 1: [52, 211, 153], 0: [186, 205, 235], "-1": [251, 146, 60] };
    const NEG = [248, 113, 113];

    let W = 0, H = 0, DPR = 1;
    let target = { x: 0, y: 0, r: 64 };
    const par = { x: 0, y: 0, tx: 0, ty: 0 };
    let dust = [], candles = [], words = [], sparks = [], rings = [];
    let price = 0, lastSpawn = 0, nextGap = 600, speedBoost = 1;
    let last = performance.now();

    function rand(a, b) { return a + Math.random() * (b - a); }
    function rgba(c, a) { return `rgba(${c[0]},${c[1]},${c[2]},${a})`; }

    function resize() {
      DPR = Math.min(window.devicePixelRatio || 1, 2);
      W = window.innerWidth;
      H = window.innerHeight;
      canvas.width = Math.round(W * DPR);
      canvas.height = Math.round(H * DPR);
      ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
      dust = Array.from({ length: Math.round((W * H) / 9000) }, () => ({
        x: Math.random() * W, y: Math.random() * H, r: rand(0.4, 1.6), a: rand(0.15, 0.6),
        tw: rand(0.5, 2), ph: rand(0, Math.PI * 2), vy: rand(-9, -3), d: rand(0.3, 1),
      }));
      candles = [];
      price = 0;
      for (let x = -10; x < W + 40; x += 15) candles.push(newCandle(x));
    }

    function newCandle(x) {
      const o = price;
      let c = o + rand(-0.17, 0.17) + Math.sin(x / 260) * 0.03;
      c = Math.max(-1, Math.min(1, c * 0.97));
      price = c;
      return { x, o, c, hi: Math.max(o, c) + rand(0.02, 0.12), lo: Math.min(o, c) - rand(0.02, 0.12) };
    }

    function spawnWord(now) {
      const [label, tone] = WORDS[(Math.random() * WORDS.length) | 0];
      const side = Math.random();
      let sx, sy;
      if (side < 0.38) { sx = -60; sy = rand(H * 0.08, H * 0.7); }
      else if (side < 0.76) { sx = W + 60; sy = rand(H * 0.08, H * 0.7); }
      else { sx = rand(W * 0.1, W * 0.9); sy = -30; }
      const mx = (sx + target.x) / 2, my = (sy + target.y) / 2;
      const nx = -(target.y - sy), ny = target.x - sx;
      const len = Math.hypot(nx, ny) || 1;
      const bend = rand(-0.45, 0.45) * Math.hypot(target.x - sx, target.y - sy);
      words.push({
        label, color: tone === -1 && Math.random() < 0.35 ? NEG : TONE[tone],
        sx, sy, cx: mx + (nx / len) * bend, cy: my + (ny / len) * bend,
        start: now, dur: rand(4200, 6800), size: rand(10.5, 13.5), trail: [],
      });
    }

    function burst(x, y, color, n, power) {
      for (let i = 0; i < n; i++) {
        const a = rand(0, Math.PI * 2), v = rand(40, 160) * power;
        sparks.push({ x, y, vx: Math.cos(a) * v, vy: Math.sin(a) * v, life: 1, decay: rand(0.9, 1.6), color, r: rand(0.8, 2.2) });
      }
    }

    function ring(color, alpha, width, speed) {
      rings.push({ r: target.r * 0.9, a: alpha, color, w: width, speed });
    }

    function bezier(w, t) {
      const u = 1 - t;
      return {
        x: u * u * w.sx + 2 * u * t * w.cx + t * t * target.x,
        y: u * u * w.sy + 2 * u * t * w.cy + t * t * target.y,
      };
    }

    function drawCandles(dt) {
      const speed = 15 * dt;
      for (const c of candles) c.x -= speed;
      while (candles.length && candles[0].x < -20) candles.shift();
      while (!candles.length || candles[candles.length - 1].x < W + 20) {
        candles.push(newCandle((candles.length ? candles[candles.length - 1].x : 0) + 15));
      }
      const base = H * 0.87 + par.y * 10, amp = H * 0.07, ox = par.x * 18;
      const y = (v) => base - v * amp;
      ctx.lineWidth = 1;
      for (const c of candles) {
        const up = c.c >= c.o;
        const col = up ? "rgba(45,212,191," : "rgba(248,113,113,";
        const x = c.x + ox;
        ctx.strokeStyle = col + "0.30)";
        ctx.beginPath();
        ctx.moveTo(x + 4, y(c.hi));
        ctx.lineTo(x + 4, y(c.lo));
        ctx.stroke();
        ctx.fillStyle = col + "0.26)";
        const top = y(Math.max(c.o, c.c)), h = Math.max(1.5, Math.abs(y(c.o) - y(c.c)));
        ctx.fillRect(x, top, 8, h);
      }
      // smoothed price line with glow
      ctx.save();
      ctx.beginPath();
      let ema = candles[0] ? candles[0].c : 0;
      candles.forEach((c, i) => {
        ema = ema + (c.c - ema) * 0.25;
        const px = c.x + 4 + ox, py = y(ema);
        if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
      });
      ctx.strokeStyle = "rgba(103,232,249,0.45)";
      ctx.lineWidth = 1.6;
      ctx.shadowColor = "rgba(103,232,249,0.8)";
      ctx.shadowBlur = 10;
      ctx.stroke();
      ctx.restore();
      // fade the tape into the floor
      const g = ctx.createLinearGradient(0, H * 0.74, 0, H);
      g.addColorStop(0, "rgba(3,6,12,0)");
      g.addColorStop(1, "rgba(3,6,12,0.55)");
      ctx.fillStyle = g;
      ctx.fillRect(0, H * 0.74, W, H * 0.26);
    }

    function drawDust(dt, t) {
      for (const p of dust) {
        p.y += p.vy * dt;
        if (p.y < -4) { p.y = H + 4; p.x = Math.random() * W; }
        const a = p.a * (0.55 + 0.45 * Math.sin(t * p.tw + p.ph));
        ctx.fillStyle = `rgba(170,200,255,${a.toFixed(3)})`;
        ctx.beginPath();
        ctx.arc(p.x + par.x * 30 * p.d, p.y + par.y * 20 * p.d, p.r, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    function drawRings(dt) {
      ctx.save();
      for (const r of rings) {
        r.r += r.speed * dt;
        r.a -= dt * 0.42;
        if (r.a <= 0) continue;
        ctx.strokeStyle = rgba(r.color, r.a);
        ctx.lineWidth = r.w;
        ctx.beginPath();
        ctx.arc(target.x, target.y, r.r, 0, Math.PI * 2);
        ctx.stroke();
      }
      ctx.restore();
      rings = rings.filter((r) => r.a > 0);
    }

    function drawWords(dt, now) {
      ctx.save();
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      for (const w of words) {
        const p = Math.min(1, ((now - w.start) / w.dur) * speedBoost);
        w.p = Math.max(w.p || 0, p);
        const e = w.p * w.p * (3 - 2 * w.p); // smoothstep: drift in, then get pulled
        const pos = bezier(w, e);
        w.trail.push(pos);
        if (w.trail.length > 14) w.trail.shift();
        const fadeIn = Math.min(1, w.p / 0.12);
        const shrink = 1 - 0.55 * Math.max(0, (w.p - 0.7) / 0.3);
        if (w.trail.length > 1) {
          ctx.beginPath();
          w.trail.forEach((q, i) => (i ? ctx.lineTo(q.x, q.y) : ctx.moveTo(q.x, q.y)));
          ctx.strokeStyle = rgba(w.color, 0.18 * fadeIn);
          ctx.lineWidth = 1;
          ctx.stroke();
        }
        ctx.font = `600 ${(w.size * shrink).toFixed(1)}px ui-monospace, SFMono-Regular, Menlo, monospace`;
        ctx.shadowColor = rgba(w.color, 0.9);
        ctx.shadowBlur = 10;
        ctx.fillStyle = rgba(w.color, 0.92 * fadeIn);
        ctx.fillText(w.label, pos.x, pos.y);
        if (w.p >= 1) {
          w.done = true;
          burst(target.x, target.y, w.color, 7, 0.8);
          ring(w.color, 0.32, 1.4, 120);
        }
      }
      ctx.restore();
      words = words.filter((w) => !w.done);
    }

    function drawSparks(dt) {
      for (const s of sparks) {
        s.x += s.vx * dt;
        s.y += s.vy * dt;
        s.vx *= 0.96;
        s.vy *= 0.96;
        s.life -= dt * s.decay;
        if (s.life <= 0) continue;
        ctx.fillStyle = rgba(s.color, Math.max(0, s.life));
        ctx.beginPath();
        ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
        ctx.fill();
      }
      sparks = sparks.filter((s) => s.life > 0);
    }

    function frame(now) {
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      par.x += (par.tx - par.x) * 0.06;
      par.y += (par.ty - par.y) * 0.06;
      ctx.clearRect(0, 0, W, H);
      drawDust(dt, now / 1000);
      drawCandles(dt);
      drawRings(dt);
      if (!document.hidden && now - lastSpawn > nextGap) {
        spawnWord(now);
        lastSpawn = now;
        nextGap = rand(380, 820) / speedBoost;
      }
      drawWords(dt, now);
      drawSparks(dt);
      requestAnimationFrame(frame);
    }

    resize();
    window.addEventListener("resize", resize);
    if (reduceMotion) {
      drawDust(0, 0);
      drawCandles(0);
    } else {
      requestAnimationFrame((t) => { last = t; frame(t); });
    }

    return {
      setTarget(el) {
        if (!el) return;
        const r = el.getBoundingClientRect();
        if (!r.width) return;
        target = { x: r.left + r.width / 2, y: r.top + r.height * 0.47, r: Math.max(26, r.width * 0.42) };
      },
      parallax(x, y) { par.tx = x; par.ty = y; },
      pulse() { ring([94, 234, 212], 0.5, 2, 150); },
      reject() { ring([248, 113, 113], 0.55, 2, 210); burst(target.x, target.y, [248, 113, 113], 18, 0.9); },
      celebrate() {
        speedBoost = 2.2;
        for (let i = 0; i < 3; i++) window.setTimeout(() => ring([52, 211, 153], 0.7, 2.5, 260), i * 160);
        burst(target.x, target.y, [52, 211, 153], 60, 1.6);
      },
    };
  }
})();
