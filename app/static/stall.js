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
  const btn = form.querySelector("button"); btn.disabled = true;
  try {
    const res = await fetch(`/api/stalls/${stallId}/orders`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items, pickup_date: dateInput.value, buyer_name: form.elements.buyer_name.value }),
    });
    const d = await res.json();
    if (!res.ok) throw new Error(d.message || "Could not place the order");
    window.location = d.paypal_approval_url;
  } catch (err) { status.textContent = err.message; btn.disabled = false; }
});
