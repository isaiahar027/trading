// Among Us Trading Desk dashboard. Vanilla JS, no dependencies.
// All dynamic content is inserted with textContent / DOM APIs (never innerHTML) and nothing is inline,
// so the page works under a strict Content-Security-Policy.
"use strict";

(() => {
  const TOKEN_KEY = "amongus-desk.token";
  const SVG_NS = "http://www.w3.org/2000/svg";
  const $ = (id) => document.getElementById(id);

  const state = {
    token: readToken(),
    data: null,
    selectedMeeting: null,
    lastMeetingId: null,
    refreshTimer: null,
    streamAbort: null,
    streamBackoff: 1000,
  };

  // ------------------------------------------------------------------------------------------ utils

  function readToken() {
    try {
      return localStorage.getItem(TOKEN_KEY) || "";
    } catch {
      return "";
    }
  }

  function saveToken(token) {
    state.token = token;
    try {
      if (token) localStorage.setItem(TOKEN_KEY, token);
      else localStorage.removeItem(TOKEN_KEY);
    } catch {
      /* private mode: keep it in memory only */
    }
  }

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : String(value));
    }
    for (const child of children) {
      if (child === null || child === undefined) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function fmtMoney(value, currency = "") {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
    const n = Number(value);
    return `${n < 0 ? "−" : ""}${Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}${currency ? " " + currency : ""}`;
  }

  function fmtPrice(value) {
    if (value === null || value === undefined) return "—";
    return Number(value).toPrecision(7).replace(/\.?0+$/, "");
  }

  function fmtTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function signClass(value) {
    if (value === null || value === undefined) return "";
    return Number(value) > 0 ? "pos" : Number(value) < 0 ? "neg" : "";
  }

  function toast(message, isError = false) {
    const t = $("toast");
    t.textContent = message;
    t.classList.toggle("error", isError);
    window.clearTimeout(toast.timer);
    toast.timer = window.setTimeout(() => {
      t.textContent = "";
    }, 6000);
  }

  // ------------------------------------------------------------------------------------------ crewmate SVG

  function shade(hex, factor) {
    const m = /^#?([0-9a-f]{6})$/i.exec(hex || "");
    if (!m) return "#555";
    const n = parseInt(m[1], 16);
    const ch = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((c) => Math.max(0, Math.min(255, Math.round(c * factor))));
    return `#${ch.map((c) => c.toString(16).padStart(2, "0")).join("")}`;
  }

  function crewmate(color, ejected = false) {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 64 72");
    svg.setAttribute("aria-hidden", "true");
    const add = (tag, attrs) => {
      const node = document.createElementNS(SVG_NS, tag);
      for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
      svg.append(node);
      return node;
    };
    const outline = { stroke: "#0b0f14", "stroke-width": 3, "stroke-linejoin": "round" };
    add("rect", { x: 4, y: 26, width: 13, height: 26, rx: 5, fill: shade(color, 0.72), ...outline });
    add("path", {
      d: "M14 24 C14 9 24 3 33 3 C44 3 52 10 52 24 L52 62 C52 66 49 68 46 68 L40 68 C37 68 36 66 36 63 L36 56 L30 56 L30 63 C30 66 29 68 26 68 L20 68 C17 68 14 66 14 62 Z",
      fill: color,
      ...outline,
    });
    add("path", { d: "M14 50 C24 54 42 54 52 50", fill: "none", stroke: shade(color, 0.7), "stroke-width": 3 });
    add("ellipse", { cx: 39, cy: 23, rx: 13, ry: 8.5, fill: ejected ? "#58616b" : "#9fe3f2", ...outline });
    add("ellipse", { cx: 43, cy: 20, rx: 5, ry: 2.4, fill: "#ffffffcc" });
    return svg;
  }

  // ------------------------------------------------------------------------------------------ API

  async function api(path, { method = "GET", body } = {}) {
    const headers = { Accept: "application/json" };
    if (state.token) headers.Authorization = `Bearer ${state.token}`;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
      credentials: "omit",
    });
    if (response.status === 401) {
      askForToken("That token was not accepted.");
      throw new Error("unauthorized");
    }
    if (response.status === 429) throw new Error("Too many failed logins from this device; wait a few minutes.");
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    if (!response.ok) {
      const msg = (payload && (payload.error || (payload.detail && JSON.stringify(payload.detail)))) || `HTTP ${response.status}`;
      throw new Error(msg);
    }
    return payload;
  }

  async function refresh() {
    if (!state.token) return askForToken();
    try {
      const data = await api("/api/state");
      state.data = data;
      render(data);
    } catch (err) {
      if (err.message !== "unauthorized") setConn(false, err.message);
    }
  }

  function scheduleRefresh(delay = 250) {
    window.clearTimeout(state.refreshTimer);
    state.refreshTimer = window.setTimeout(refresh, delay);
  }

  // SSE over fetch() so the token stays in a header (EventSource can't send headers).
  async function stream() {
    if (!state.token) return;
    if (state.streamAbort) state.streamAbort.abort();
    const controller = new AbortController();
    state.streamAbort = controller;
    try {
      const response = await fetch("/api/events", {
        headers: { Authorization: `Bearer ${state.token}`, Accept: "text/event-stream" },
        signal: controller.signal,
        cache: "no-store",
        credentials: "omit",
      });
      if (response.status === 401) return askForToken("That token was not accepted.");
      if (!response.ok || !response.body) throw new Error(`stream HTTP ${response.status}`);
      setConn(true);
      state.streamBackoff = 1000;
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buffer.search(/\r?\n\r?\n/)) >= 0) {
          const chunk = buffer.slice(0, idx);
          buffer = buffer.slice(idx).replace(/^\r?\n\r?\n/, "");
          handleSse(chunk);
        }
      }
      throw new Error("stream closed");
    } catch (err) {
      if (controller.signal.aborted) return;
      setConn(false, err.message);
      window.setTimeout(stream, state.streamBackoff);
      state.streamBackoff = Math.min(state.streamBackoff * 2, 30000);
    }
  }

  function handleSse(chunk) {
    let type = "message";
    const data = [];
    for (const line of chunk.split(/\r?\n/)) {
      if (line.startsWith(":")) continue; // keep-alive comment
      if (line.startsWith("event:")) type = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    }
    if (!data.length) return;
    let payload = null;
    try {
      payload = JSON.parse(data.join("\n"));
    } catch {
      return;
    }
    if (type === "meeting" && payload && payload.data) {
      showBanner(payload.data.symbol, payload.data.decision);
    }
    scheduleRefresh(type === "bar" ? 1500 : 150);
  }

  function setConn(ok, title = "") {
    const c = $("conn");
    c.classList.toggle("ok", ok);
    c.classList.toggle("bad", !ok);
    c.title = ok ? "live" : `disconnected ${title}`.trim();
  }

  // ------------------------------------------------------------------------------------------ rendering

  function render(d) {
    const mode = String(d.mode || "").toLowerCase();
    const badge = $("mode-badge");
    badge.textContent = mode.toUpperCase() + (d.dry_run ? " · DRY RUN" : "");
    badge.className = `badge mode mode-${mode}`;
    $("subtitle").textContent = `${d.venue} venue · updated ${fmtTime(d.now)}`;
    $("kill-badge").hidden = !(d.kill_switch && d.kill_switch.engaged);
    document.title = `${d.kill_switch && d.kill_switch.engaged ? "⛔ " : ""}${mode.toUpperCase()} · Among Us Trading Desk`;

    renderAlerts(d);
    renderStats(d);
    renderReconcile(d.reconcile || []);
    renderCrew(d.crew || []);
    renderMeetings(d.meetings || []);
    renderPositions(d.positions || [], (d.equity && d.equity.currency) || "");
    renderIntents(d.intents || []);
    renderHealth(d.health || {}, d.instruments || []);
    renderControls(d);
  }

  function renderAlerts(d) {
    const box = $("alerts");
    clear(box);
    if (d.kill_switch && d.kill_switch.engaged) {
      box.append(el("div", { class: "alert bad", text: `Kill switch engaged${d.kill_switch.reason ? `: ${d.kill_switch.reason}` : ""}. No new trades.` }));
    }
    for (const reason of d.degraded || []) box.append(el("div", { class: "alert", text: reason }));
    if ((d.reconcile || []).length) {
      box.append(el("div", { class: "alert bad", text: `${d.reconcile.length} order(s) need you to check the terminal. Trading is halted until resolved.` }));
    }
  }

  function renderStats(d) {
    const eq = d.equity || {};
    const lim = d.limits || {};
    $("stat-equity").textContent = fmtMoney(eq.estimate, eq.currency);
    const pnl = (Number(eq.realized_today) || 0) + (Number(eq.unrealized) || 0);
    const pnlEl = $("stat-pnl");
    pnlEl.textContent = fmtMoney(pnl, eq.currency);
    pnlEl.className = `value ${signClass(pnl)}`;
    $("stat-trades").textContent = `${lim.trades_today ?? "—"} / ${lim.max_trades_per_day ?? "—"}`;
    $("stat-positions").textContent = `${lim.open_positions ?? "—"} / ${lim.max_open_positions ?? "—"}`;
    const loss = Number(lim.daily_loss_pct || 0);
    $("stat-loss").textContent = `${loss.toFixed(2)}% / ${Number(lim.max_daily_loss_pct || 0).toFixed(2)}%`;
    const llm = d.llm || {};
    $("stat-llm").textContent = llm.enabled ? `${llm.calls ?? 0}${llm.max_calls_per_day ? " / " + llm.max_calls_per_day : ""}` : "off";
  }

  function renderReconcile(items) {
    const panel = $("reconcile-panel");
    const list = $("reconcile-list");
    panel.hidden = items.length === 0;
    clear(list);
    for (const item of items) {
      const priceInput = el("input", { type: "number", step: "any", min: "0", inputmode: "decimal", placeholder: "fill price (optional)", "aria-label": "Fill price" });
      list.append(
        el(
          "div",
          { class: "reconcile-card" },
          el("strong", { text: `${item.symbol || ""} ${item.side || ""} ${item.lots || ""} lots · ${item.status}` }),
          el("span", { text: `${item.kind} ${item.id} · ${fmtTime(item.created_at)}` }),
          item.message ? el("span", { class: "hint", text: item.message }) : null,
          el(
            "div",
            { class: "control-row" },
            priceInput,
            el("button", {
              class: "btn",
              type: "button",
              text: "It filled",
              onclick: () => reconcile(item.id, "filled", priceInput.value),
            }),
            el("button", {
              class: "btn warn",
              type: "button",
              text: "Nothing happened",
              onclick: () => reconcile(item.id, "not_filled", ""),
            }),
          ),
        ),
      );
    }
  }

  function renderCrew(crew) {
    const box = $("crew");
    clear(box);
    for (const member of crew) {
      const ejected = member.vote === "EJECT";
      const avatar = el("div", { class: "avatar" });
      avatar.append(crewmate(member.color, ejected));
      box.append(
        el(
          "article",
          { class: `crewmate${ejected ? " ejected" : ""}` },
          avatar,
          el(
            "div",
            {},
            el("div", { class: "name", text: member.name }),
            el("div", { class: "role", text: member.role }),
            el("span", { class: `vote vote-${member.vote}`, text: member.vote }),
          ),
          el("p", { class: "summary", text: member.summary || "Waiting for the next meeting." }),
        ),
      );
    }
    const latest = (state.data && state.data.meetings && state.data.meetings[0]) || null;
    $("crew-hint").textContent = latest ? `last meeting ${fmtTime(latest.called_at)} · ${latest.symbol}` : "no meeting yet";
  }

  function renderMeetings(meetings) {
    const select = $("meeting-select");
    const ids = meetings.map((m) => m.id);
    if (meetings.length && meetings[0].id !== state.lastMeetingId) {
      state.lastMeetingId = meetings[0].id;
      state.selectedMeeting = meetings[0].id;
    }
    if (!ids.includes(state.selectedMeeting)) state.selectedMeeting = ids[0] || null;
    clear(select);
    for (const m of meetings) {
      const option = el("option", { value: m.id, text: `${fmtTime(m.called_at)} · ${m.symbol} · ${m.decision}` });
      if (m.id === state.selectedMeeting) option.selected = true;
      select.append(option);
    }
    const meeting = meetings.find((m) => m.id === state.selectedMeeting);
    const summary = $("meeting-summary");
    const chat = $("chat");
    clear(summary);
    clear(chat);
    if (!meeting) {
      chat.append(el("li", { text: "No emergency meetings yet. Cyan Scout calls one when a breakout closes." }));
      return;
    }
    summary.append(
      el("span", { class: `decision decision-${meeting.decision}`, text: meeting.decision.replace("_", " ") }),
      meeting.lots ? el("span", { text: `${meeting.lots} lots` }) : null,
      el("span", { class: "hint", text: `${meeting.trigger} · ${meeting.symbol}` }),
    );
    const colors = {};
    for (const member of (state.data && state.data.crew) || []) colors[member.name] = member.color;
    for (const line of meeting.transcript || []) {
      const li = el("li", { text: line });
      const speaker = Object.keys(colors).find((name) => line.includes(`${name}:`));
      if (line.includes("SYSTEM:")) li.classList.add("system");
      else if (speaker) li.style.borderLeftColor = colors[speaker];
      chat.append(li);
    }
  }

  function renderPositions(positions, currency) {
    const body = $("positions");
    clear(body);
    if (!positions.length) {
      body.append(el("tr", {}, el("td", { class: "empty", colspan: 10, text: "No open positions." })));
      return;
    }
    for (const p of positions) {
      let pnl = null;
      if (p.last_price !== null && p.last_price !== undefined && p.entry_price) {
        pnl = p.unrealized_pnl ?? null;
      }
      body.append(
        el(
          "tr",
          {},
          el("td", { text: p.symbol }),
          el("td", { text: p.side }),
          el("td", { text: p.lots }),
          el("td", { text: fmtPrice(p.entry_price) }),
          el("td", { text: fmtPrice(p.stop_price) }),
          el("td", { text: fmtPrice(p.target_price) }),
          el("td", { text: fmtPrice(p.last_price) }),
          el("td", { class: signClass(pnl), text: pnl === null ? "—" : fmtMoney(pnl, currency) }),
          el("td", { class: `status-${p.status}`, text: p.status + (p.protective_at_broker ? " · SL/TP at broker" : "") }),
          el("td", { text: fmtTime(p.opened_at) }),
        ),
      );
    }
  }

  function renderIntents(rows) {
    const body = $("intents");
    clear(body);
    if (!rows.length) {
      body.append(el("tr", {}, el("td", { class: "empty", colspan: 7, text: "No orders yet." })));
      return;
    }
    for (const row of rows) {
      const it = row.intent || {};
      const rep = row.report || {};
      body.append(
        el(
          "tr",
          {},
          el("td", { text: fmtTime(it.created_at) }),
          el("td", { text: it.purpose }),
          el("td", { text: it.symbol }),
          el("td", { text: it.side || "—" }),
          el("td", { text: it.lots || "—" }),
          el("td", { class: `status-${row.status}`, text: row.status }),
          el("td", { class: "detail", text: [rep.message, rep.stage ? `(${rep.stage})` : ""].filter(Boolean).join(" ") }),
        ),
      );
    }
  }

  function renderHealth(health, instruments) {
    const list = $("health");
    clear(list);
    const venue = health.venue;
    if (venue && Array.isArray(venue.checks)) {
      for (const check of venue.checks) {
        list.append(el("li", { class: check.ok ? "ok" : "bad", text: `${check.name}: ${check.detail}` }));
      }
    } else {
      list.append(el("li", { text: "Venue health not checked yet." }));
    }
    if (health.feed) {
      const f = health.feed;
      const detail = f.last_error ? `error: ${f.last_error}` : f.name || "ok";
      list.append(el("li", { class: f.last_error ? "bad" : "ok", text: `feed: ${detail}` }));
    }
    const inst = $("instruments");
    clear(inst);
    for (const i of instruments) {
      const last = i.last_bar ? `${fmtPrice(i.last_bar.close)} @ ${fmtTime(i.last_bar.start)}` : "no data yet";
      const flags = [i.enabled ? "enabled" : "disabled", i.in_session ? "in session" : "out of session", i.specs_verified ? "specs verified" : "specs NOT verified"];
      inst.append(el("li", { text: `${i.symbol}: ${last} · ${flags.join(" · ")}` }));
    }
  }

  function renderControls(d) {
    const select = $("meeting-symbol");
    const current = select.value;
    clear(select);
    for (const i of d.instruments || []) {
      if (!i.enabled) continue;
      select.append(el("option", { value: i.symbol, text: i.symbol, selected: i.symbol === current }));
    }
    $("force-wrap").hidden = String(d.mode).toLowerCase() !== "paper";
    const killed = d.kill_switch && d.kill_switch.engaged;
    $("btn-kill").disabled = Boolean(killed);
    $("btn-resume").disabled = !killed;
  }

  // ------------------------------------------------------------------------------------------ banner

  function showBanner(symbol, decision) {
    const banner = $("meeting-banner");
    const crew = $("banner-crew");
    clear(crew);
    crew.append(crewmate("#c51111"));
    $("banner-symbol").textContent = `${symbol || ""}${decision ? " · " + String(decision).replace("_", " ") : ""}`;
    banner.hidden = false;
    // restart the CSS animation
    banner.classList.remove("again");
    void banner.offsetWidth;
    window.clearTimeout(showBanner.timer);
    showBanner.timer = window.setTimeout(() => {
      banner.hidden = true;
    }, 2700);
  }

  // ------------------------------------------------------------------------------------------ dialogs & actions

  function askForToken(message = "") {
    const dialog = $("token-dialog");
    $("token-error").textContent = message;
    if (!dialog.open) dialog.showModal();
  }

  function confirmDialog({ title, text, inputLabel = null, okLabel = "OK", expect = null }) {
    return new Promise((resolve) => {
      const dialog = $("confirm-dialog");
      $("confirm-title").textContent = title;
      $("confirm-text").textContent = text;
      $("confirm-ok").textContent = okLabel;
      const wrap = $("confirm-input-wrap");
      const input = $("confirm-input");
      wrap.hidden = inputLabel === null;
      $("confirm-input-label").textContent = inputLabel || "";
      input.value = "";
      const form = $("confirm-form");
      const onSubmit = (event) => {
        event.preventDefault();
        const ok = event.submitter && event.submitter.value === "ok";
        if (ok && expect !== null && input.value.trim() !== expect) {
          $("confirm-text").textContent = `${text} (type ${expect} exactly)`;
          return;
        }
        form.removeEventListener("submit", onSubmit);
        dialog.close();
        resolve(ok ? input.value.trim() || true : null);
      };
      form.addEventListener("submit", onSubmit);
      dialog.addEventListener("cancel", () => {
        form.removeEventListener("submit", onSubmit);
        resolve(null);
      }, { once: true });
      dialog.showModal();
      if (inputLabel !== null) input.focus();
    });
  }

  async function act(label, fn) {
    try {
      await fn();
      toast(`${label}: done`);
    } catch (err) {
      toast(`${label}: ${err.message}`, true);
    } finally {
      scheduleRefresh(50);
    }
  }

  async function reconcile(id, outcome, price) {
    const words = outcome === "filled" ? "it FILLED" : "NOTHING happened";
    const ok = await confirmDialog({ title: "Reconcile order", text: `You checked UniTrader and ${words}?`, okLabel: "Yes" });
    if (!ok) return;
    const fill = price === "" ? null : Number(price);
    await act("Reconcile", () => api("/api/reconcile", { method: "POST", body: { id, outcome, fill_price: Number.isFinite(fill) ? fill : null } }));
  }

  function bindControls() {
    $("token-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const token = $("token-input").value.trim();
      if (token.length < 8) {
        $("token-error").textContent = "That looks too short.";
        return;
      }
      saveToken(token);
      $("token-dialog").close();
      refresh();
      stream();
    });

    $("meeting-select").addEventListener("change", (event) => {
      state.selectedMeeting = event.target.value;
      if (state.data) renderMeetings(state.data.meetings || []);
    });

    $("btn-kill").addEventListener("click", async () => {
      const reason = await confirmDialog({ title: "Engage kill switch", text: "No new trades until you resume. Open positions keep their stops.", inputLabel: "Reason", okLabel: "Engage" });
      if (reason === null) return;
      await act("Kill switch", () => api("/api/kill", { method: "POST", body: { reason: reason === true ? "dashboard" : reason } }));
    });

    $("btn-resume").addEventListener("click", async () => {
      const ok = await confirmDialog({ title: "Resume trading", text: "Type RESUME to let the crew trade again.", inputLabel: "Confirmation", okLabel: "Resume", expect: "RESUME" });
      if (ok === null) return;
      await act("Resume", () => api("/api/resume", { method: "POST", body: { confirm: "RESUME" } }));
    });

    const panic = $("btn-panic");
    let holdTimer = null;
    const cancelHold = () => {
      panic.classList.remove("holding");
      window.clearTimeout(holdTimer);
      holdTimer = null;
    };
    panic.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      panic.classList.add("holding");
      holdTimer = window.setTimeout(async () => {
        cancelHold();
        await act("PANIC", () => api("/api/panic", { method: "POST", body: { confirm: "PANIC" } }));
      }, 2000);
    });
    for (const ev of ["pointerup", "pointerleave", "pointercancel"]) panic.addEventListener(ev, cancelHold);
    panic.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") event.preventDefault();
    });

    $("meeting-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const symbol = $("meeting-symbol").value;
      if (!symbol) return;
      const force = $("force-wrap").hidden ? "" : $("meeting-force").value;
      await act("Emergency meeting", () => api("/api/meeting", { method: "POST", body: { symbol, force_side: force || null } }));
    });

    $("equity-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const equity = Number($("equity-input").value);
      if (!(equity > 0)) return toast("Enter the balance shown in UniTrader.", true);
      await act("Equity sync", () => api("/api/equity", { method: "POST", body: { equity } }));
    });
  }

  // ------------------------------------------------------------------------------------------ boot

  function boot() {
    $("brand-icon").append(crewmate("#c51111"));
    bindControls();
    if (!state.token) askForToken();
    else {
      refresh();
      stream();
    }
    window.setInterval(() => {
      if (state.token && document.visibilityState === "visible") refresh();
    }, 15000);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") scheduleRefresh(10);
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
