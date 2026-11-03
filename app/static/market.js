// Market run builder: one basket across stalls, one PayPal approval.
(() => {
  const form = document.getElementById("market-form");
  const dateEl = document.getElementById("market-date");
  const totalEl = document.getElementById("market-total"), summaryEl = document.getElementById("market-summary");
  const submit = document.getElementById("market-submit"), errEl = document.getElementById("market-error");
  const result = document.getElementById("market-result");
  const money = (c) => "$" + (c / 100).toFixed(2);
  const open = {};
  // One id per basket: if the same basket is sent twice (double click, slow network) the server
  // returns the first market run instead of creating a second one.
  const newRef = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));
  let clientRef = newRef();
  let placed = false;  // once a market run exists, this basket is locked until the buyer starts a new one
  let sending = false;

  // Default to the next Saturday at least 2 days away (markets are mostly weekend).
  const d = new Date(); d.setDate(d.getDate() + 2);
  while (d.getDay() !== 6) d.setDate(d.getDate() + 1);
  dateEl.value = d.toISOString().slice(0, 10);
  dateEl.min = new Date().toISOString().slice(0, 10);

  function basket() {
    const stalls = [];
    for (const sec of document.querySelectorAll(".market-stall")) {
      const items = [...sec.querySelectorAll(".qty")].map((i) => ({ item_id: +i.dataset.item, quantity: +i.value || 0, price: +i.dataset.price }))
        .filter((x) => x.quantity > 0);
      if (items.length) stalls.push({ stall_id: sec.dataset.stall, items });
    }
    return stalls;
  }

  function update() {
    const b = basket();
    const total = b.reduce((s, st) => s + st.items.reduce((a, i) => a + i.quantity * i.price, 0), 0);
    totalEl.textContent = money(total);
    const closed = b.filter((st) => open[st.stall_id] === false);
    summaryEl.textContent = b.length ? `${b.length} stall${b.length > 1 ? "s" : ""} in your basket` + (closed.length ? `, ${closed.length} closed that day` : "") : "Nothing in your basket yet";
    submit.disabled = placed || sending || !b.length || closed.length > 0;
  }

  function lock(basket) {
    placed = true;
    form.querySelectorAll("input").forEach((i) => { i.disabled = true; });
    dateEl.disabled = true; document.getElementById("market-name").disabled = true;
    submit.disabled = true;
    submit.textContent = basket.status === "paid" ? "Paid" : "Market run created";
    if (!document.getElementById("market-new")) {
      const again = document.createElement("button");
      again.type = "button"; again.id = "market-new"; again.className = "btn";
      again.textContent = "Start a new market run";
      again.onclick = () => {
        placed = false; clientRef = newRef();
        form.querySelectorAll("input").forEach((i) => { i.disabled = false; if (i.classList.contains("qty")) i.value = 0; });
        dateEl.disabled = false; document.getElementById("market-name").disabled = false;
        submit.textContent = "Pay every stall with one PayPal approval";
        result.innerHTML = ""; again.remove(); loadAvailability();
      };
      submit.insertAdjacentElement("afterend", again);
    }
  }

  async function loadAvailability() {
    await Promise.all([...document.querySelectorAll(".market-stall")].map(async (sec) => {
      const note = sec.querySelector(".avail");
      const res = await fetch(`/api/stalls/${sec.dataset.stall}/availability?date=${dateEl.value}`).catch(() => null);
      if (!res || !res.ok) { note.textContent = ""; return; }
      const a = await res.json();
      open[sec.dataset.stall] = a.open;
      sec.classList.toggle("closed", !a.open);
      note.textContent = a.open ? `Open ${a.weekday}: ${a.pickup_windows.join(", ")}` : (a.reason_closed || "Closed that day");
      for (const it of a.items) {
        const el = sec.querySelector(`.left[data-item="${it.item_id}"]`);
        if (el) el.textContent = it.remaining === null ? "" : `${it.remaining} left that day`;
      }
    }));
    update();
  }

  // Reflect the payment result on the main button once PayPal finishes.
  OpenStallCheckout.onMarketUpdate((b) => {
    if (!placed) return;
    submit.textContent = { paid: "Paid", partly_paid: "Partly paid", confirming: "Confirming payment...", closed: "Not paid" }[b.status] || "Waiting for PayPal approval";
  });
  form.addEventListener("input", update);
  dateEl.addEventListener("change", loadAvailability);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (placed || sending) return;
    sending = true;
    errEl.textContent = ""; submit.disabled = true; submit.textContent = "Creating your market run...";
    const body = { pickup_date: dateEl.value, buyer_name: document.getElementById("market-name").value, client_ref: clientRef,
                   stalls: basket().map((s) => ({ stall_id: s.stall_id, items: s.items.map(({ item_id, quantity }) => ({ item_id, quantity })) })) };
    try {
      const res = await fetch("/api/market", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const data = await res.json();
      if (!res.ok) { errEl.textContent = data.message || (data.detail && "Check your basket and try again.") || "Could not create the market run."; return; }
      lock(data);
      result.innerHTML = "";
      const el = document.createElement("div");
      result.appendChild(el);
      OpenStallCheckout.marketCard(data, el);
      el.scrollIntoView({ behavior: "smooth" });
      const btn = el.querySelector(".pay-all");
      if (btn) btn.click();  // open PayPal straight away; the user just asked to pay
    } catch (_) { errEl.textContent = "Network error, please retry. You will not be charged twice."; }
    finally {
      sending = false;
      if (!placed) submit.textContent = "Pay every stall with one PayPal approval";
      update();
    }
  });
  loadAvailability();
})();
