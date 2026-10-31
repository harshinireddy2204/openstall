const table = document.querySelector("table.orders");
if (table) table.addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-action]");
  if (!btn) return;
  const action = btn.dataset.action, body = { key: table.dataset.key, action };
  if (action === "ship") {
    body.tracking_number = prompt("Tracking number?") || "";
    if (!body.tracking_number) return;
    body.carrier = prompt("Carrier (USPS, UPS, FEDEX)?", "USPS") || "USPS";
  }
  btn.disabled = true;
  const res = await fetch(`/api/stalls/${table.dataset.stall}/orders/${btn.dataset.order}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (res.ok) location.reload(); else { const d = await res.json(); alert(d.message || "Action failed"); btn.disabled = false; }
});

const custom = document.querySelector("table.custom");
if (custom) custom.addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target, btn = f.querySelector("button");
  btn.disabled = true;
  const res = await fetch(`/api/stalls/${custom.dataset.stall}/custom/${f.dataset.ref}/quote`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key: custom.dataset.key, amount: parseFloat(f.amount.value), deposit_percent: parseInt(f.deposit_percent.value, 10) }),
  });
  if (res.ok) location.reload(); else { const d = await res.json(); alert(d.message || "Could not send invoice"); btn.disabled = false; }
});
