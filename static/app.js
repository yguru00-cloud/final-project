/* Cart Commute front end: form, live agent progress, results, map, and the plan helper chat. */
(() => {
  "use strict";

  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const money = (v) => (v == null ? "—" : "$" + Number(v).toFixed(2));
  const hourLabel = (h) => (h == null ? "" : `${((h + 11) % 12) + 1}${h < 12 ? "am" : "pm"}`);
  const MODE = { walk: { icon: "🚶", name: "Walk" }, shuttle: { icon: "🚌", name: "Yale shuttle" }, uber: { icon: "🚕", name: "Uber" }, zipcar: { icon: "🚗", name: "Zipcar" } };
  const SOURCE = {
    weekly_sale: ["sale", "Sale"], website_listed: ["web", "Website"], estimated: ["est", "Estimate"], missing: ["miss", "No price"],
  };
  const QUICK = [];  // filled from /api/health with the items that have store prices on file
  const AGENT_NAMES = { list_reader: "List Reader", price_scout: "Price Scout", trip_planner: "Trip Planner", trip_advisor: "Trip Advisor", plan_helper: "Plan Helper" };

  let plan = null;
  let map = null;
  let chatHistory = [];

  // ----------------------------------------------------------------- micro-interactions
  document.addEventListener("pointerdown", (e) => {
    const el = e.target.closest(".btn, .chip, .toggle, .tab");
    if (!el) return;
    const r = el.getBoundingClientRect();
    const d = Math.max(r.width, r.height);
    const s = document.createElement("span");
    s.className = "ripple";
    s.style.cssText = `width:${d}px;height:${d}px;left:${e.clientX - r.left - d / 2}px;top:${e.clientY - r.top - d / 2}px`;
    el.appendChild(s);
    setTimeout(() => s.remove(), 650);
  });

  function toast(msg, kind = "") {
    const t = document.createElement("div");
    t.className = `toast ${kind}`;
    t.textContent = msg;
    $("#toasts").appendChild(t);
    setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 300); }, 4200);
  }

  function countUp(el, to, ms = 900) {
    const start = performance.now();
    const step = (now) => {
      const p = Math.min(1, (now - start) / ms);
      el.textContent = money(to * (1 - Math.pow(1 - p, 3)));
      if (p < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  function coinBurst(fromEl) {
    const r = fromEl.getBoundingClientRect();
    for (let i = 0; i < 14; i++) {
      const c = document.createElement("span");
      c.className = "coin";
      c.textContent = i % 3 ? "🪙" : "✨";
      c.style.left = r.left + r.width / 2 + "px";
      c.style.top = r.top + r.height / 2 + "px";
      c.style.setProperty("--dx", (Math.random() * 220 - 110).toFixed(0) + "px");
      c.style.setProperty("--dy", (-60 - Math.random() * 140).toFixed(0) + "px");
      c.style.setProperty("--rot", (Math.random() * 360).toFixed(0) + "deg");
      document.body.appendChild(c);
      setTimeout(() => c.remove(), 1100);
    }
  }

  // ----------------------------------------------------------------------- form setup
  fetch("/api/health").then((r) => r.json()).then((h) => {
    const pill = $("#status-pill");
    pill.classList.add(h.has_key ? "ok" : "bad");
    $("#status-text").textContent = h.has_key ? `Live · ${h.stores} stores` : "API key missing";
    if ((h.well_compared || []).length) {
      $("#quick-items").innerHTML = `<span class="chips-label" title="Store-website prices are on file for these at most stores, so the comparison is most accurate. Anything else works too: it uses this week's sales or estimates.">✓ Best compared</span>`;
      h.well_compared.forEach((w) => addQuick(w.item, `${w.stores}/${w.of} stores have a price on file`));
    }
  }).catch(() => { $("#status-pill").classList.add("bad"); $("#status-text").textContent = "Offline"; });

  const shopping = $("#shopping");
  const updateCounter = () => { $("#counter").textContent = `${shopping.value.length}/1500`; };
  shopping.addEventListener("input", updateCounter);

  const quick = $("#quick-items");
  QUICK.forEach((q) => addQuick(q));
  function addQuick(q, tip) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "chip";
    if (tip) b.title = tip;
    b.textContent = "+ " + q;
    b.addEventListener("click", () => {
      const cur = shopping.value.trim();
      shopping.value = cur ? `${cur.replace(/[,\s]+$/, "")}, ${q}` : q;
      updateCounter();
      b.classList.add("added");
      setTimeout(() => b.classList.remove("added"), 600);
    });
    $("#quick-items").appendChild(b);
  }

  $$("#address-chips .chip").forEach((c) => c.addEventListener("click", () => { $("#address").value = c.dataset.address; }));
  $$("#modes .toggle").forEach((t) => t.addEventListener("click", () => {
    const on = $$("#modes .toggle.on");
    if (t.classList.contains("on") && on.length === 1) return toast("Keep at least one way to get there.");
    t.classList.toggle("on");
  }));
  const bindRange = (id, out, fmt) => { const el = $(id); const o = $(out); const f = () => (o.textContent = fmt(el.value)); el.addEventListener("input", f); f(); };
  bindRange("#days", "#days-out", (v) => v);
  bindRange("#carry", "#carry-out", (v) => v);
  bindRange("#timevalue", "#timevalue-out", (v) => "$" + v);

  // ------------------------------------------------------------------------- run plan
  $("#plan-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = {
      address: $("#address").value.trim(),
      shopping_list: shopping.value.trim(),
      days_ahead: +$("#days").value,
      modes: $$("#modes .toggle.on").map((t) => t.dataset.mode),
      has_costco: $("#has-costco").checked,
      has_bjs: $("#has-bjs").checked,
      carry_limit_lb: +$("#carry").value,
      time_value_per_hour: +$("#timevalue").value,
      zipcar_hourly: $("#zipcar").value ? +$("#zipcar").value : null,
    };
    if (!body.address || !body.shopping_list) return toast("Add your address and at least one item.");
    startRun();
    try {
      const res = await fetch("/api/plan", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(typeof err.detail === "string" ? err.detail : "The planner could not start.");
      }
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, i).trim();
          buf = buf.slice(i + 1);
          if (line) onEvent(JSON.parse(line));
        }
      }
    } catch (err) {
      failRun(err.message);
    }
  });

  let t0 = 0, timerId = 0;
  function startRun() {
    $("#empty").classList.add("hidden");
    $("#results").classList.add("hidden");
    $("#run").classList.remove("hidden");
    $("#feed").innerHTML = "";
    $$(".stage").forEach((s) => s.classList.remove("active", "done"));
    const go = $("#go");
    go.disabled = true;
    go.classList.add("loading");
    $(".btn-label", go).textContent = "Planning";
    t0 = performance.now();
    clearInterval(timerId);
    timerId = setInterval(() => { $("#timer").textContent = ((performance.now() - t0) / 1000).toFixed(1) + "s"; }, 100);
    $("#run").scrollIntoView({ behavior: "smooth", block: "start" });
  }
  function endRun() {
    clearInterval(timerId);
    const go = $("#go");
    go.disabled = false;
    go.classList.remove("loading");
    $(".btn-label", go).textContent = "Plan my trip";
  }
  function failRun(msg) {
    endRun();
    toast(msg, "error");
    feed("warn", "Stopped", msg);
  }
  function stage(agent, state) {
    const el = $(`.stage[data-agent="${agent}"]`);
    if (!el) return;
    if (state === "active") { $$(".stage.active").forEach((s) => { if (s !== el) { s.classList.remove("active"); s.classList.add("done"); } }); }
    el.classList.remove("active", "done");
    el.classList.add(state);
  }
  function feed(kind, tag, text) {
    const row = document.createElement("div");
    row.className = "feed-row";
    row.innerHTML = `<span class="tag ${kind}">${esc(tag)}</span><span>${esc(text)}</span>`;
    const f = $("#feed");
    f.appendChild(row);
    f.scrollTop = f.scrollHeight;
  }

  function onEvent(ev) {
    switch (ev.type) {
      case "agent_start":
        stage(ev.agent, "active");
        feed("agent", AGENT_NAMES[ev.agent] || ev.agent, "started");
        break;
      case "agent_done": {
        const u = ev.usage || {};
        feed("agent", AGENT_NAMES[ev.agent] || ev.agent, `done · ${(u.input_tokens || 0) + (u.output_tokens || 0)} tokens · $${(u.cost_usd || 0).toFixed(5)}`);
        if (ev.agent !== "trip_advisor") stage(ev.agent, "done");
        break;
      }
      case "agent_skip":
        stage(ev.agent, "done");
        feed("agent", AGENT_NAMES[ev.agent] || ev.agent, "skipped: " + ev.reason);
        break;
      case "tool":
        if (ev.tool === "routes" || ev.tool === "weather") stage("trip_planner", "active");
        if (ev.tool === "search_prices" && !$('.stage[data-agent="price_scout"]').classList.contains("done")) stage("price_scout", "active");
        feed("tool", ev.tool, `${ev.args ? Object.values(ev.args).join(", ") + " → " : ""}${ev.summary || ""}${ev.stale ? " (saved copy)" : ""}`);
        break;
      case "items":
        feed("agent", "List", ev.items.map((i) => i.display).join(" · "));
        break;
      case "guard":
        if (ev.check === "input_screen" && ev.suspicious) feed("warn", "Guard", "Instruction-like text found in your input. It is treated as data only.");
        else if (ev.check === "list_reader" && (ev.ignored || []).length) feed("warn", "Guard", "Ignored: " + ev.ignored.join(" · "));
        else if (ev.check === "offer_grounding") feed("guard", "Guard", `${ev.accepted} offer matches verified${ev.rejected ? `, ${ev.rejected} rejected` : ""}`);
        else if (ev.check === "price_grounding") feed(ev.ungrounded.length ? "warn" : "guard", "Guard", ev.ungrounded.length ? `Unverified amounts ${ev.ungrounded.map(money).join(", ")} sent back for a rewrite` : "Every dollar amount checked against the data");
        break;
      case "optimizer":
        stage("trip_planner", "done");
        feed("tool", "optimizer", `${ev.options} plans compared · best total ${money(ev.total)}`);
        break;
      case "result":
        $$(".stage").forEach((s) => { s.classList.remove("active"); s.classList.add("done"); });
        endRun();
        setTimeout(() => showPlan(ev.plan), 350);
        break;
      case "error":
        failRun(ev.message);
        break;
    }
  }

  // --------------------------------------------------------------------------- render
  function showPlan(p) {
    plan = p;
    chatHistory = [];
    $("#chat-log").innerHTML = "";
    $("#run").classList.add("hidden");
    $("#results").classList.remove("hidden");
    renderHero(p);
    renderReview(p);
    renderAdvice(p);
    renderMap(p);
    renderBasket(p.best);
    renderCompare(p);
    renderWeek(p);
    renderHood(p);
    selectTab("basket");
    $("#chat-fab").classList.remove("hidden");
    setupChatSuggestions(p);
    $("#results").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function shuttleSteps(t) {
    const b = t.breakdown;
    if (!b) return "";
    const step = (icon, label, min) => `<span class="step"><span>${icon}</span>${label}<b>${min}′</b></span>`;
    return `<div class="steps" title="Loop time is not published by Yale: assumed 50 min, split by distance. Going out you watch the tracker app; coming back you wait for the shuttle to come around again.">
      ${step("🚶", "to stop", b.walk_to_stop)}${step("⏳", "wait (tracker)", b.wait_out)}${step("🚌", "ride", b.ride_out)}
      ${step("🚶", "stop ↔ store", b.walk_at_stores)}${step("🛒", "shop", b.shopping)}${step("⏳", "wait for loop", b.wait_back)}${step("🚌", "ride back", b.ride_back)}${step("🚶", "home", b.walk_home)}
    </div>`;
  }

  // when would valuing your time change the answer? (computed by code from the plans' totals and minutes)
  function flipNotes(p) {
    const be = p.break_even || {};
    const name = (o) => `${esc(o.stores)} by ${MODE[o.mode].name} (${money(o.total)}, ${o.minutes} min, ${esc(o.day)})`;
    const lines = [];
    if (be.faster_above) lines.push(`If your time is worth more than <b>${money(be.faster_above.per_hour)}/hour</b>, the faster ${name(be.faster_above)} is the better deal.`);
    if (be.cheaper_below) lines.push(`If your time is worth less than <b>${money(be.cheaper_below.per_hour)}/hour</b>, the cheaper ${name(be.cheaper_below)} is the better deal.`);
    if (!lines.length) return "";
    return `<div class="flip" title="Break-even = extra dollars ÷ hours saved, from the plans' own totals and times. Set your time value in Trip settings to rank plans with it.">⚖️ ${lines.join("<br>⚖️ ")}</div>`;
  }

  function timeSplit(o) {
    const verb = { walk: "walking", shuttle: "shuttle + walking", uber: "in the car", zipcar: "driving" }[o.transport.mode] || "travel";
    return `${o.travel_minutes} min ${verb} + ${o.shop_minutes} min shopping`;
  }

  function renderHero(p) {
    const b = p.best, a = p.advice || {};
    const mode = MODE[b.transport.mode];
    const save = p.savings_vs_baseline;
    const stores = b.stores.map((s) => s.brand).join(" + ");
    $("#hero").innerHTML = `
      <div class="eyebrow">Best plan · ${esc(b.day_label)} · ${esc(hourLabel(b.start_hour))}</div>
      <h2>${esc(a.headline || "")}${save > 0.009 ? `<span class="save-badge" id="save-badge">Save ${money(save)}</span>`
        : (p.minutes_saved_vs_baseline > 0 ? `<span class="save-badge">⏱ ${p.minutes_saved_vs_baseline} min faster</span>` : "")}</h2>
      <div class="hero-row">
        <div class="stat big"><div class="k">Total</div><div class="v" id="total-num">$0.00</div><div class="s">groceries + trip</div></div>
        <div class="stat"><div class="k">Groceries</div><div class="v">${money(b.basket)}</div><div class="s">${b.store_priced_items}/${b.lines.length} store prices</div></div>
        <div class="stat"><div class="k">${mode.icon} ${mode.name}</div><div class="v">${money(b.transport_cost)}</div><div class="s">${b.transport.kind === "estimated" ? "estimate" : "free"} · ${timeSplit(b)}</div></div>
        ${p.time_value_per_hour > 0 ? `<div class="stat"><div class="k">⏱ Your time</div><div class="v">${money(b.time_cost)}</div><div class="s">${b.total_minutes} min × ${money(p.time_value_per_hour)}/h → ${money(b.total_with_time)} incl. time</div></div>` : ""}
        <div class="stat"><div class="k">🛒 Store</div><div class="v" style="font-size:17px">${esc(stores)}</div><div class="s">${esc(b.stores.map((s) => s.address).join(" · "))}</div></div>
        <div class="stat"><div class="k">${wxIcon(b.weather.summary)} Weather</div><div class="v" style="font-size:17px">${b.weather.temp_f != null ? Math.round(b.weather.temp_f) + "°F" : "—"}</div><div class="s">${esc(b.weather.summary)} · rain ${b.weather.rain_pct ?? 0}%</div></div>
      </div>
      ${flipNotes(p)}
      <div class="verified">🛡️ ${a.note ? esc(a.note) : a.fallback ? "Plain summary shown (the AI text did not pass the price check)" : "Every dollar amount above was checked against the source data"}</div>`;
    countUp($("#total-num"), b.total);
    if (save > 0.009) setTimeout(() => coinBurst($("#save-badge")), 500);
  }

  function renderReview(p) {
    const groups = p.review || [];
    const box = $("#review");
    if (!groups.length) { box.classList.add("hidden"); return; }
    const LETTERS = "ABCDEFGHIJ";
    const delta = (total) => {
      if (total == null) return '<span class="rv-delta">not available</span>';
      const d = Math.round((total - p.best.total) * 100) / 100;
      return Math.abs(d) < 0.005 ? '<span class="rv-delta same">same total</span>'
        : `<span class="rv-delta ${d > 0 ? "up" : "down"}">${d > 0 ? "+" : "−"}${money(Math.abs(d))}</span>`;
    };
    const row = (g, letter, o, isRegular) => `
      <button type="button" class="rv-opt ${o.selected ? "on" : ""}" data-key="${esc(g.item_key)}" data-offer="${isRegular ? "" : esc(o.offer_id)}" ${o.selected ? "disabled" : ""}>
        <span class="rv-radio">${o.selected ? "●" : "○"}</span>
        <span class="rv-letter">${letter}</span>
        <span class="rv-main"><b>${isRegular ? "Regular · " : ""}${esc(o.product || "regular product")}</b>
          <span class="prod">${esc(o.store_brand || "")}${o.unit_text ? " · " + esc(o.unit_text) : ""}${isRegular ? "" : ` · <span class="reason">${esc(o.reason)}</span>`}</span></span>
        <span class="rv-cost" title="${o.units_by === "ai" ? "Size not listed, so the amount is an AI estimate" : "Price for the amount on your list"}">${o.units_by === "ai" ? "≈ " : ""}${money(o.cost)}</span>
        <span class="rv-plan">${o.selected ? "<b>in your plan</b>" : "plan would be"}<br><b>${money(o.plan_total)}</b> ${o.selected ? "" : delta(o.plan_total)}
          <small>${esc(o.plan_stores || "")}</small></span>
      </button>`;
    box.classList.remove("hidden");
    box.innerHTML = `
      <div class="review-head">
        <div><h3>🔀 Other versions found <span class="count">${groups.length}</span></h3>
          <p>Your plan uses the <b>regular</b> product (A). We also found other versions, like lactose-free or instant.
          Pick one if you'd rather buy it: you'll see the plan's new total before you choose, and you can switch back to A anytime.</p></div>
      </div>
      <div class="review-groups">${groups.map((g) => `
        <div class="review-group"><div class="review-item">${esc(g.item)}</div>
          ${row(g, "A", g.current, true)}
          ${g.options.map((o, i) => row(g, LETTERS[i + 1], o, false)).join("")}
        </div>`).join("")}</div>`;
  }
  document.addEventListener("click", (e) => {
    const b = e.target.closest(".rv-opt");
    if (b && plan && !b.disabled) replan("version", { item_key: b.dataset.key, offer_id: b.dataset.offer || null }, b);
  });

  function renderAdvice(p) {
    const a = p.advice || {};
    $("#reasons").innerHTML = (a.reasons || []).map((r) => `<li>${esc(r)}</li>`).join("");
    $("#tips").innerHTML = (a.tips || []).map((t) => `<div class="note tip">💡 <span>${esc(t)}</span></div>`).join("");
    $("#cautions").innerHTML = (a.cautions || []).map((t) => `<div class="note caution">⚠️ <span>${esc(t)}</span></div>`).join("");
  }

  function pin(cls, emoji) {
    return L.divIcon({ className: "", html: `<div class="pin ${cls}"><span>${emoji}</span></div>`, iconSize: [30, 30], iconAnchor: [15, 30], popupAnchor: [0, -28] });
  }
  async function renderMap(p) {
    if (!window.L) return;
    if (map) map.remove();
    map = L.map("map", { scrollWheelZoom: false }).setView([p.home.lat, p.home.lon], 12);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 18, attribution: "© OpenStreetMap" }).addTo(map);
    const chosen = new Set(p.best.stores.map((s) => s.id));
    const pts = [[p.home.lat, p.home.lon]];
    L.marker([p.home.lat, p.home.lon], { icon: pin("home", "🏠"), zIndexOffset: 1000 }).addTo(map).bindPopup(`<b>Home</b><br>${esc(p.home.label)}`);
    try {
      const { stores } = await (await fetch("/api/stores")).json();
      stores.forEach((s) => {
        const isChosen = chosen.has(s.id);
        L.marker([s.lat, s.lon], { icon: pin(isChosen ? "chosen" : "store", isChosen ? "⭐" : "🛒"), zIndexOffset: isChosen ? 900 : 0 })
          .addTo(map).bindPopup(`<b>${esc(s.brand)}</b><br>${esc(s.address)}<br><small>${esc(s.hours || "hours not listed")}</small>`);
        if (isChosen) pts.push([s.lat, s.lon]);
      });
    } catch (_) { /* the map still shows home */ }
    p.best.stores.forEach((s) => L.polyline([[p.home.lat, p.home.lon], [s.lat, s.lon]], { color: "#1f6f4a", weight: 3, dashArray: "6 8" }).addTo(map));
    map.fitBounds(pts, { padding: [40, 40], maxZoom: 13 });
    setTimeout(() => map.invalidateSize(), 200);
  }

  function badge(l) {
    if (l.not_compared) return `<span class="badge miss" title="${esc(l.basis)}">Not compared</span>`;
    const [cls, label] = SOURCE[l.source] || SOURCE.missing;
    return `<span class="badge ${cls}" title="${esc(l.basis)}">${label}</span>`;
  }
  function productCell(l) {
    const opts = l.options || [];
    if (opts.length < 2) return `<div class="prod">${esc(l.product || "")}${l.unit_text ? " · " + esc(l.unit_text) : ""}</div>`;
    return `<select class="swap" data-key="${esc(l.key)}" data-brand="${esc(l.store_brand)}" title="Other matching products at ${esc(l.store_brand)}">
      ${opts.map((o, i) => `<option value="${esc(o.offer_id)}" title="${esc(o.product)} · ${esc(o.unit_text)}" ${o.offer_id === l.offer_id ? "selected" : ""}>${money(o.cost)}${i === 0 ? " ✓ cheapest" : ""} · ${esc(o.product.length > 38 ? o.product.slice(0, 37) + "…" : o.product)}</option>`).join("")}
    </select>${l.user_choice ? '<span class="badge web" style="margin-left:6px">your pick</span>' : ""}`;
  }
  function qtyCell(l) {
    const it = (plan?.items || []).find((x) => x.key === l.key);
    if (!it) return `<b>${esc(l.item)}</b>`;
    return `<div class="qty-row"><b>${esc(l.item)}</b>
      <span class="stepper" data-key="${esc(it.key)}" data-qty="${it.qty}" data-step="${it.step || 1}">
        <button type="button" class="step-btn" data-dir="-1" aria-label="Less">−</button>
        <button type="button" class="step-btn" data-dir="1" aria-label="More">+</button>
      </span></div>`;
  }
  function basketTable(o) {
    const rows = o.lines.map((l) => `
      <tr>
        <td>${qtyCell(l)}${productCell(l)}${l.units_by === "ai" ? '<div class="prod" title="The size of this product could not be read, so the AI estimated how many to buy">≈ amount estimated by AI</div>' : ""}</td>
        <td>${esc(l.store_brand)}</td>
        <td>${badge(l)}${l.valid_to ? `<div class="prod">until ${esc(l.valid_to)}</div>` : ""}</td>
        <td class="num">${l.price == null ? "—" : money(l.price) + (l.units && l.units !== 1 ? ` × ${+l.units.toFixed(2)}` : "")}</td>
        <td class="num">${money(l.cost)}</td>
      </tr>`).join("");
    const t = o.transport;
    return `<table>
      <thead><tr><th>Item</th><th>Store</th><th>Price source</th><th class="num">Price</th><th class="num">Cost</th></tr></thead>
      <tbody>${rows}
        <tr><td><b>${MODE[t.mode].icon} ${MODE[t.mode].name}</b><div class="prod">${esc(t.detail)}</div>${shuttleSteps(t)}</td><td></td>
          <td><span class="badge ${t.kind === "free" ? "free" : "est"}" title="${esc(t.basis || "")}">${t.kind === "free" ? "Free" : "Estimate"}</span></td><td></td><td class="num">${money(t.cost)}</td></tr>
        <tr class="total-row"><td>Total you pay</td><td></td><td></td><td></td><td class="num">${money(o.total)}</td></tr>
        ${plan && plan.time_value_per_hour > 0 ? `
        <tr><td><b>⏱ Your time</b><div class="prod">${timeSplit(o)} = ${o.total_minutes} min × ${money(plan.time_value_per_hour)}/h</div></td><td></td>
          <td><span class="badge est" title="Set in Trip settings: what an hour of your time is worth to you. Used to rank plans; you don't pay it.">Your setting</span></td><td></td><td class="num">${money(o.time_cost)}</td></tr>
        <tr class="total-row"><td>Total incl. your time <span class="prod">(main ranking number; rain, heavy bags and estimated prices also count)</span></td><td></td><td></td><td></td><td class="num">${money(o.total_with_time)}</td></tr>` : ""}
      </tbody></table>
      ${o.missing.length ? `<p class="note caution">⚠️ No price found for: ${esc(o.missing.join(", "))}. They are not in the total.</p>` : ""}
      ${(o.not_compared || []).length ? `<p class="note tip">ℹ️ No store has a price on file for: ${esc(o.not_compared.join(", "))}. Buy it wherever you go; it is left out of every plan's total alike, so it doesn't change the ranking.</p>` : ""}`;
  }
  function renderBasket(o) {
    $("#tab-basket").innerHTML = `<div class="legend">
        <span><span class="badge sale">Sale</span> this week's flyer</span>
        <span><span class="badge web">Website</span> store website price</span>
        <span><span class="badge est">Estimate</span> U.S. average or ride estimate</span>
        <span style="margin-left:auto">Hover a badge to see the source</span></div>${basketTable(o)}`;
  }

  function renderCompare(p) {
    const opts = [p.best, ...p.alternatives];
    if (p.baseline && !opts.some((o) => o.plan_id === p.baseline.plan_id)) opts.push(p.baseline);
    const max = Math.max(...opts.map((o) => o.total));
    $("#tab-compare").innerHTML = `
      <div class="legend"><span><i style="background:var(--green-2)"></i>Groceries</span><span><i style="background:var(--orange)"></i>Getting there</span><span style="margin-left:auto">Click a plan to see its basket</span></div>
      ${opts.map((o) => {
        const isBase = p.baseline && o.plan_id === p.baseline.plan_id;
        const m = MODE[o.transport.mode];
        return `<div class="compare-row ${o.plan_id === p.best.plan_id ? "best" : ""}" data-id="${o.plan_id}">
          <div><b>${esc(o.stores.map((s) => s.brand).join(" + "))}</b> ${o.plan_id === p.best.plan_id ? '<span class="badge sale">Best</span>' : ""}${isBase ? ' <span class="badge miss">Nearest store</span>' : ""}
            <div class="meta">${m.icon} ${m.name} · ${esc(o.day_label)} ${esc(hourLabel(o.start_hour))} · ${timeSplit(o)}${p.time_value_per_hour > 0 ? ` · ${money(o.total_with_time)} incl. your time` : ""}${o.missing.length ? ` · ${o.missing.length} missing` : ""}</div></div>
          <div class="bar-wrap"><div class="bar" style="width:${Math.max(18, (o.total / max) * 100)}%"><div class="b1" style="flex:${o.basket}"></div><div class="b2" style="flex:${o.transport_cost}"></div></div>
            <small>${money(o.basket)} + ${money(o.transport_cost)}</small></div>
          <div class="price">${money(o.total)}</div>
        </div><div class="compare-detail hidden" id="detail-${o.plan_id}"></div>`;
      }).join("")}`;
    $$("#tab-compare .compare-row").forEach((row) => row.addEventListener("click", () => {
      const o = opts.find((x) => x.plan_id === row.dataset.id);
      const d = $(`#detail-${o.plan_id}`);
      if (d.classList.toggle("hidden")) return;
      d.innerHTML = basketTable(o);
    }));
  }

  function wxIcon(summary = "") {
    const s = summary.toLowerCase();
    if (s.includes("thunder")) return "⛈️";
    if (s.includes("snow")) return "🌨️";
    if (s.includes("rain") || s.includes("drizzle") || s.includes("shower")) return "🌧️";
    if (s.includes("fog")) return "🌫️";
    if (s.includes("partly") || s.includes("mostly")) return "⛅";
    if (s.includes("cloud")) return "☁️";
    return "☀️";
  }
  function renderWeek(p) {
    const days = p.weather.map((d) => {
      const dt = new Date(d.date + "T12:00:00");
      return `<div class="day ${d.date === p.best.day ? "chosen" : ""}" title="${esc(d.summary)}">
        <div class="dn">${dt.toLocaleDateString("en-US", { weekday: "short" })} ${dt.getMonth() + 1}/${dt.getDate()}</div>
        <div class="ic">${wxIcon(d.summary)}</div>
        <div class="t">${Math.round(d.high_f)}° / ${Math.round(d.low_f)}°</div>
        <div class="r">💧 ${d.rain_pct ?? 0}%</div></div>`;
    }).join("");
    const dl = (p.sale_deadlines || []).map((d) => `<div class="deadline"><span>🏷️ <b>${esc(d.item)}</b> at ${esc(d.store_brand)}</span><span>sale ends ${esc(d.valid_to)}</span></div>`).join("");
    $("#tab-week").innerHTML = `<div class="week">${days}</div>
      <div class="deadlines"><h3 style="margin-top:8px">Sale deadlines</h3>${dl || '<p class="prod">No sale in your plans ends this week.</p>'}</div>
      <p class="prod" style="margin-top:12px">Forecast: Open-Meteo. Busy hours: ${esc(p.busy_note)}</p>`;
  }

  function renderHood(p) {
    const u = p.usage;
    const perRun = u.cost_usd;
    const g = p.guards;
    const fresh = p.data_freshness;
    const staleSales = (fresh.sales || []).filter((s) => s.stale).length;
    $("#tab-hood").innerHTML = `
      <div class="hood-grid">
        <div class="kpi"><div class="k">Model calls</div><div class="v">${u.requests}</div></div>
        <div class="kpi"><div class="k">Input tokens (cached)</div><div class="v">${u.input_tokens.toLocaleString()} <small style="font-size:13px;color:var(--muted)">(${u.cached_tokens.toLocaleString()})</small></div></div>
        <div class="kpi"><div class="k">Output tokens</div><div class="v">${u.output_tokens.toLocaleString()}</div></div>
        <div class="kpi"><div class="k">Cost of this plan</div><div class="v">$${perRun.toFixed(5)}</div></div>
      </div>
      <table><thead><tr><th>Agent</th><th class="num">Calls</th><th class="num">Input</th><th class="num">Cached</th><th class="num">Output</th><th class="num">Cost</th></tr></thead>
      <tbody>${u.agents.map((a) => `<tr><td>${esc(AGENT_NAMES[a.agent] || a.agent)}</td><td class="num">${a.requests}</td><td class="num">${a.input_tokens.toLocaleString()}</td><td class="num">${a.cached_tokens.toLocaleString()}</td><td class="num">${a.output_tokens.toLocaleString()}</td><td class="num">$${a.cost_usd.toFixed(5)}</td></tr>`).join("")}</tbody></table>
      <p class="prod">Model: gpt-6-luna via Portkey · $${u.rates_per_million.input}/M input, $${u.rates_per_million.cached_input}/M cached, $${u.rates_per_million.output}/M output · run budget cap $${u.budget_usd.toFixed(2)}</p>
      <h3 style="margin-top:18px">Safety checks on this run</h3>
      <div class="guard-list">
        <div class="guard-item"><span class="${g.input.suspicious ? "warn" : "ok"}">${g.input.suspicious ? "!" : "✓"}</span>Input screen: ${g.input.suspicious ? "instruction-like text found and treated as data (" + esc(g.input.matches.join(", ")) + ")" : "no instruction-like text"}</div>
        <div class="guard-item"><span class="${g.list_ignored.length ? "warn" : "ok"}">${g.list_ignored.length ? "!" : "✓"}</span>List Reader ignored: ${g.list_ignored.length ? esc(g.list_ignored.join(" · ")) : "nothing"}</div>
        <div class="guard-item"><span class="ok">✓</span>Offer grounding: every price comes from a tool result (${g.rejected_picks} unverifiable picks rejected)</div>
        <div class="guard-item"><span class="ok">✓</span>Price check: ${p.advice.fallback ? "AI text failed twice, plain summary shown" : "every $ amount in the advice matches the data"}</div>
        <div class="guard-item"><span class="ok">✓</span>Limits: ${u.requests} model calls of 25 max, $${perRun.toFixed(5)} of $${u.budget_usd.toFixed(2)} budget</div>
      </div>
      <h3 style="margin-top:18px">Data used</h3>
      <div class="guard-list">
        <div class="guard-item">🏷️ Weekly sales (Flipp, ZIP 06511): ${fresh.sales.length} searches, ${staleSales ? staleSales + " from the saved copy" : "all live"}</div>
        <div class="guard-item">🗺️ ${esc(fresh.routes_basis)}</div>
        <div class="guard-item">🌦️ Weather: Open-Meteo ${fresh.weather.live ? "(live)" : "(saved copy from " + esc(fresh.weather.fetched_at) + ")"}</div>
        <div class="guard-item">🚌 ${esc(p.shuttle.basis)}</div>
        <div class="guard-item">🚕 ${esc(p.rates.uber.basis)}</div>
        <div class="guard-item">🚗 ${esc(p.rates.zipcar.basis)}</div>
      </div>
      <p class="prod" style="margin-top:10px">Run id <span class="mono">${esc(p.id)}</span> · full step log at <a href="/api/audit?run_id=${encodeURIComponent(p.id)}" target="_blank">/api/audit</a></p>`;
  }

  // product swaps and amount changes re-rank everything on the server by code (no AI call), then redraw
  async function replan(path, body, el) {
    const before = plan.best.total;
    if (el) el.disabled = true;
    try {
      const res = await fetch(`/api/plan/${encodeURIComponent(plan.id)}/${path}`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "Could not update the plan.");
      const tab = $(".tab.active")?.dataset.tab || "basket";
      const sameStores = plan.best.stores.map((s) => s.id).join() === data.best.stores.map((s) => s.id).join();
      plan = data;
      renderHero(plan); renderReview(plan); renderAdvice(plan); if (!sameStores) renderMap(plan);
      renderBasket(plan.best); renderCompare(plan); renderWeek(plan); renderHood(plan);
      selectTab(tab);
      const diff = plan.best.total - before;
      toast(`Updated: best plan is now ${money(plan.best.total)} (${diff >= 0 ? "+" : "−"}${money(Math.abs(diff))})`);
    } catch (err) {
      toast(err.message, "error");
      if (el) el.disabled = false;
    }
  }
  document.addEventListener("change", (e) => {
    const sel = e.target.closest("select.swap");
    if (sel && plan) replan("swap", { item_key: sel.dataset.key, store_brand: sel.dataset.brand, offer_id: sel.value }, sel);
  });
  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".step-btn");
    if (!btn || !plan) return;
    const box = btn.closest(".stepper");
    const step = +box.dataset.step, qty = +box.dataset.qty;
    const next = Math.round((qty + step * +btn.dataset.dir) * 100) / 100;
    if (next <= 0) return toast("To drop an item, make a new plan without it.");
    replan("quantity", { item_key: box.dataset.key, qty: next }, btn);
  });

  function selectTab(name) {
    $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    $$(".tab-body").forEach((b) => b.classList.toggle("hidden", b.id !== "tab-" + name));
  }
  $$(".tab").forEach((t) => t.addEventListener("click", () => selectTab(t.dataset.tab)));

  // ----------------------------------------------------------------------------- chat
  function setupChatSuggestions(p) {
    const firstItem = p.items[0]?.display || "eggs";
    const other = p.alternatives[0]?.stores?.[0]?.brand || "Costco";
    const s = [`Why not ${other}?`, "What if I go on Saturday?", `Is ${p.items[0]?.search_keyword || "milk"} on sale anywhere?`, "Which prices are estimates?"];
    $("#chat-suggest").innerHTML = s.map((q) => `<button type="button" class="chip">${esc(q)}</button>`).join("");
    $$("#chat-suggest .chip").forEach((c) => c.addEventListener("click", () => sendChat(c.textContent)));
    addMsg("bot", `Hi! Ask me anything about this plan: why it picked ${p.best.stores.map((x) => x.brand).join(" + ")}, other days, or when the sale on ${firstItem} ends.`);
  }
  function addMsg(who, text, meta) {
    const m = document.createElement("div");
    m.className = `msg ${who}`;
    m.textContent = text;
    if (meta) { const s = document.createElement("span"); s.className = "meta"; s.textContent = meta; m.appendChild(s); }
    $("#chat-log").appendChild(m);
    $("#chat-log").scrollTop = $("#chat-log").scrollHeight;
    return m;
  }
  async function sendChat(text) {
    text = text.trim();
    if (!text || !plan) return;
    $("#chat-text").value = "";
    addMsg("user", text);
    const typing = document.createElement("div");
    typing.className = "msg bot typing";
    typing.innerHTML = "<i></i><i></i><i></i>";
    $("#chat-log").appendChild(typing);
    $("#chat-log").scrollTop = $("#chat-log").scrollHeight;
    try {
      const res = await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ plan_id: plan.id, message: text, history: chatHistory.slice(-8) }) });
      const data = await res.json().catch(() => ({ detail: "the helper did not answer. Please try again." }));
      typing.remove();
      if (!res.ok) throw new Error(data.detail || "Chat failed");
      const meta = [data.tools.length ? "used: " + data.tools.join(", ") : "",
        data.guard.provider_filter ? "🛡️ blocked by the AI provider's safety filter" : (data.guard.suspicious ? "🛡️ instruction-like text treated as data" : ""),
        data.grounding_blocked ? "🛡️ unverified prices blocked" : "", `$${data.usage.cost_usd.toFixed(5)}`].filter(Boolean).join(" · ");
      addMsg("bot", data.answer, meta);
      chatHistory.push({ role: "user", content: text }, { role: "assistant", content: data.answer });
    } catch (err) {
      typing.remove();
      addMsg("bot", "Sorry, " + err.message);
    }
  }
  $("#chat-fab").addEventListener("click", () => { $("#chat").classList.remove("hidden"); $("#chat-fab").classList.add("hidden"); $("#chat-text").focus(); });
  $("#chat-close").addEventListener("click", () => { $("#chat").classList.add("hidden"); $("#chat-fab").classList.remove("hidden"); });
  $("#chat-form").addEventListener("submit", (e) => { e.preventDefault(); sendChat($("#chat-text").value); });
})();
