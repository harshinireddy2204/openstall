// Opens PayPal approval in a popup so the shopper never leaves OpenStall,
// then shows the order inline. Falls back to a full-page redirect if popups are blocked.
const OpenStallCheckout = (() => {
  const esc = (v) => String(v ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const KEY = "openstall_orders";
  const LIVE = ["paid", "ready", "shipped", "payment_review", "capturing"];
  const watchers = {};
  function remembered() { try { return JSON.parse(localStorage.getItem(KEY) || "[]"); } catch (_) { return []; } }
  function remember(id) {
    try { const ids = remembered().filter((x) => x !== id); ids.unshift(id); localStorage.setItem(KEY, JSON.stringify(ids.slice(0, 50))); } catch (_) {}
  }
  function fmtTime(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return isNaN(d) ? "" : d.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  }
  function stepsHtml(steps) {
    if (!steps || !steps.length) return "";
    return `<ol class="tracker">${steps.map((s) => `<li class="${s.state}"><span class="dot"></span><span class="label">${esc(s.label)}</span><span class="when">${esc(fmtTime(s.at))}</span></li>`).join("")}</ol>`;
  }
  // Keep a card in sync with seller actions (ready, picked up, refund...) until the order is final.
  function track(orderId, el) {
    if (watchers[orderId]) return;
    watchers[orderId] = setInterval(async () => {
      if (!document.body.contains(el)) { clearInterval(watchers[orderId]); delete watchers[orderId]; return; }
      const res = await fetch(`/api/orders/${orderId}`).catch(() => null);
      if (!res || !res.ok) return;
      const order = await res.json();
      const before = el.dataset.status;
      card(order, el);
      if (before && before !== order.status) { el.classList.add("flash"); setTimeout(() => el.classList.remove("flash"), 1500); }
      if (!LIVE.includes(order.status) && order.status !== "pending_payment") { clearInterval(watchers[orderId]); delete watchers[orderId]; }
    }, 4000);
  }
  const DONE = ["paid", "ready", "picked_up", "shipped", "payment_review", "refunded", "sold_out", "cancelled", "payment_failed", "needs_attention", "expired"];

  function card(order, el) {
    el = el || document.createElement("div");
    const keep = el.classList.contains("msg") ? " msg bot" : "";
    el.className = "order-card status-" + order.status + keep;
    el.dataset.orderId = order.order_id; el.dataset.status = order.status;
    remember(order.order_id);
    const lines = (order.lines || []).map((l) => `<li>${esc(l.quantity)} × ${esc(l.name)}</li>`).join("");
    const good = ["paid", "ready", "picked_up", "shipped"].includes(order.status);
    el.innerHTML = `
      <div class="order-card-head"><span class="badge ${good ? "ok" : order.status === "pending_payment" ? "wait" : "warn"}">${esc(order.status_label)}</span>
        <span class="muted">${esc(order.order_id)}</span></div>
      <strong>${esc(order.stall_name)}</strong>
      ${stepsHtml(order.steps)}
      <ul>${lines}</ul>
      <div class="order-card-foot"><strong>${esc(order.total)}</strong>
        <span class="muted">Pickup ${esc(order.pickup_date)} · ${esc(order.pickup_window)}</span></div>
      ${good && order.pickup_address ? `<p class="muted">${esc(order.pickup_address)}</p>` : ""}
      ${order.error ? `<p class="error">${esc(order.error)}</p>` : ""}
      ${order.tracking ? `<p class="muted">Tracking: ${esc(order.tracking.carrier)} ${esc(order.tracking.number)}</p>` : ""}
      ${order.status === "pending_payment" && order.paypal_approval_url && el.dataset.inMarket !== "1" ? `<button class="btn primary pay-btn">${order.market_order_id ? "Approve market run payment in PayPal" : "Approve payment in PayPal"}</button>` : ""}
      <a class="muted small-link" href="/order/${esc(order.order_id)}" target="_blank" rel="noopener">Order page</a>`;
    const btn = el.querySelector(".pay-btn");
    if (btn) btn.onclick = () => pay(order.paypal_approval_url, order.order_id, el);
    return el;
  }

  async function refresh(orderId, el, confirm) {
    const res = await fetch(`/api/orders/${orderId}${confirm ? "/confirm" : ""}`, { method: confirm ? "POST" : "GET" });
    const order = await res.json();
    if (res.ok) card(order, el);
    return order;
  }

  function pay(url, orderId, el) {
    const w = 480, h = 720;
    const popup = window.open(url, "openstall_paypal", `width=${w},height=${h},left=${(screen.width - w) / 2},top=${(screen.height - h) / 2}`);
    if (!popup) { window.location = url; return; }  // popup blocked: classic redirect flow
    const status = el.querySelector(".pay-btn");
    if (status) { status.disabled = true; status.textContent = "Waiting for PayPal..."; }
    let finished = false;
    const finish = async () => {
      if (finished) return; finished = true;
      clearInterval(timer); window.removeEventListener("message", onMsg);
      const order = await refresh(orderId, el, true);
      el.scrollIntoView({ behavior: "smooth", block: "nearest" });
      track(orderId, el);
      return order;
    };
    const onMsg = (e) => { if (e.origin === location.origin && e.data && e.data.type === "openstall-order" && e.data.order_id === orderId) finish(); };
    window.addEventListener("message", onMsg);
    const timer = setInterval(async () => {
      if (popup.closed) return finish();
      const o = await refresh(orderId, el, false).catch(() => null);
      if (o && DONE.includes(o.status)) { try { popup.close(); } catch (_) {} finish(); }
    }, 2500);
  }

  // ---------------------------------------------------------------- market runs
  // One basket across several stalls. In "single" mode one PayPal approval pays every seller.
  function marketCard(basket, el) {
    el = el || document.createElement("div");
    const keep = el.classList.contains("msg") ? " msg bot" : "";
    el.className = "market-card status-" + basket.status + keep;
    el.dataset.marketId = basket.market_order_id;
    const good = basket.status === "paid";
    const single = basket.mode === "single";
    el.innerHTML = `
      <div class="order-card-head"><span class="badge ${good ? "ok" : basket.status === "pending_payment" ? "wait" : "warn"}">${esc(basket.status_label)}</span>
        <span class="muted">${esc(basket.market_order_id)}</span></div>
      <strong>Market run · ${esc(basket.stall_count)} stall${basket.stall_count === 1 ? "" : "s"} · ${esc(basket.total)}</strong>
      <p class="muted">Pickup ${esc(basket.pickup_date)}. ${single ? "One PayPal approval pays each seller directly." : "Each stall has its own PayPal approval."}</p>
      ${basket.error ? `<p class="error">${esc(basket.error)}</p>` : ""}
      ${single && basket.paypal_approval_url ? `<button class="btn primary pay-all">Approve one payment in PayPal (${esc(basket.total)})</button>` : ""}
      <div class="market-orders"></div>
      <a class="muted small-link" href="/market/${esc(basket.market_order_id)}" target="_blank" rel="noopener">Market run page</a>`;
    const box = el.querySelector(".market-orders");
    for (const o of basket.orders || []) {
      const c = document.createElement("div");
      if (single) c.dataset.inMarket = "1";  // the market-level button pays this stall; no per-stall button
      card(o, c);
      box.appendChild(c);
      if (LIVE.includes(o.status) || o.status === "pending_payment") track(o.order_id, c);
    }
    const btn = el.querySelector(".pay-all");
    if (btn) btn.onclick = () => payMarket(basket, el);
    return el;
  }

  async function refreshMarket(id, el, confirm) {
    const res = await fetch(`/api/market/${id}${confirm ? "/confirm" : ""}`, { method: confirm ? "POST" : "GET" });
    const basket = await res.json();
    if (res.ok) marketCard(basket, el);
    return basket;
  }

  function payMarket(basket, el) {
    const url = basket.paypal_approval_url, id = basket.market_order_id;
    const childIds = (basket.orders || []).map((o) => o.order_id);
    const w = 480, h = 720;
    const popup = window.open(url, "openstall_paypal", `width=${w},height=${h},left=${(screen.width - w) / 2},top=${(screen.height - h) / 2}`);
    if (!popup) { window.location = url; return; }
    const btn = el.querySelector(".pay-all");
    if (btn) { btn.disabled = true; btn.textContent = "Waiting for PayPal..."; }
    let finished = false;
    const finish = async () => {
      if (finished) return; finished = true;
      clearInterval(timer); window.removeEventListener("message", onMsg);
      await refreshMarket(id, el, true);
      el.scrollIntoView({ behavior: "smooth", block: "nearest" });
    };
    const onMsg = (e) => {
      if (e.origin !== location.origin || !e.data) return;
      if ((e.data.type === "openstall-market" && e.data.market_order_id === id) || (e.data.type === "openstall-order" && childIds.includes(e.data.order_id))) finish();
    };
    window.addEventListener("message", onMsg);
    const timer = setInterval(async () => {
      if (popup.closed) return finish();
      const r = await fetch(`/api/market/${id}`).catch(() => null);
      const b = r && r.ok ? await r.json() : null;
      if (b && b.status !== "pending_payment") { try { popup.close(); } catch (_) {} finish(); }
    }, 2500);
  }

  return { card, pay, refresh, track, remember, remembered, marketCard, refreshMarket };
})();
