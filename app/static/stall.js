const form = document.getElementById("order-form");
const stallId = form.dataset.stall;
const dateInput = form.elements.pickup_date;
const avail = document.getElementById("availability");
const status = document.getElementById("order-status");
dateInput.min = new Date().toISOString().slice(0, 10);

dateInput.addEventListener("change", async () => {
  avail.textContent = "Checking...";
  const res = await fetch(`/api/stalls/${stallId}/availability?date=${dateInput.value}`);
  const d = await res.json();
  if (!res.ok) { avail.textContent = d.message; return; }
  avail.textContent = d.open
    ? `Open ${d.weekday}: ${d.pickup_windows.join(", ")}. ` + d.items.filter((i) => i.remaining !== null).map((i) => `${i.name}: ${i.remaining} left`).join(" · ")
    : d.reason_closed;
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  status.textContent = "";
  const items = [...form.querySelectorAll(".qty")].map((i) => ({ item_id: +i.dataset.item, quantity: +i.value })).filter((l) => l.quantity > 0);
  if (!items.length) { status.textContent = "Choose at least one item."; return; }
  const btn = form.querySelector("button");
  if (btn.disabled) return;
  const btnLabel = btn.dataset.label || (btn.dataset.label = btn.textContent);
  btn.disabled = true;
  try {
    const res = await fetch(`/api/stalls/${stallId}/orders`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items, pickup_date: dateInput.value, buyer_name: form.elements.buyer_name.value }),
    });
    const d = await res.json();
    if (!res.ok) throw new Error(d.message || "Could not place the order");
    const box = document.getElementById("order-result");
    box.innerHTML = ""; box.classList.remove("hidden");
    const el = OpenStallCheckout.card(d);
    box.appendChild(el);
    OpenStallCheckout.pay(d.paypal_approval_url, d.order_id, el);
    // Lock this order: paying again happens on the card below, not by placing a second order.
    form.querySelectorAll("input").forEach((i) => { i.disabled = true; });
    btn.textContent = "Order placed";
    if (!document.getElementById("order-new")) {
      const again = document.createElement("button");
      again.type = "button"; again.id = "order-new"; again.className = "btn";
      again.textContent = "Order something else";
      again.onclick = () => {
        form.querySelectorAll("input").forEach((i) => { i.disabled = false; if (i.classList.contains("qty")) i.value = 0; });
        btn.disabled = false; btn.textContent = btnLabel; box.innerHTML = ""; box.classList.add("hidden"); again.remove();
      };
      btn.insertAdjacentElement("afterend", again);
    }
  } catch (err) { status.textContent = err.message; btn.disabled = false; }
});
