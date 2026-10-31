// Opens PayPal approval in a popup so the shopper never leaves OpenStall,
// then shows the order inline. Falls back to a full-page redirect if popups are blocked.
const OpenStallCheckout = (() => {
  const esc = (v) => String(v ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const DONE = ["paid", "ready", "picked_up", "shipped", "payment_review", "refunded", "sold_out", "cancelled", "payment_failed", "needs_attention", "expired"];

  function card(order, el) {
    el = el || document.createElement("div");
    el.className = "order-card status-" + order.status;
    const lines = (order.lines || []).map((l) => `<li>${esc(l.quantity)} × ${esc(l.name)}</li>`).join("");
    const good = ["paid", "ready", "picked_up", "shipped"].includes(order.status);
    el.innerHTML = `
      <div class="order-card-head"><span class="badge ${good ? "ok" : order.status === "pending_payment" ? "wait" : "warn"}">${esc(order.status_label)}</span>
        <span class="muted">${esc(order.order_id)}</span></div>
      <strong>${esc(order.stall_name)}</strong>
      <ul>${lines}</ul>
      <div class="order-card-foot"><strong>${esc(order.total)}</strong>
        <span class="muted">Pickup ${esc(order.pickup_date)} · ${esc(order.pickup_window)}</span></div>
      ${good && order.pickup_address ? `<p class="muted">${esc(order.pickup_address)}</p>` : ""}
      ${order.error ? `<p class="error">${esc(order.error)}</p>` : ""}
      ${order.status === "pending_payment" && order.paypal_approval_url ? `<button class="btn primary pay-btn">Approve payment in PayPal</button>` : ""}
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

  return { card, pay, refresh };
})();
