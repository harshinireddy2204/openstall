const $ = (s, el = document) => el.querySelector(s);
const extractForm = $("#extract-form"), stallForm = $("#stall-form");
const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun", "any"];
const esc = (v) => String(v ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function itemRow(it = {}) {
  const tr = document.createElement("tr");
  tr.innerHTML = `<td><input class="i-name" value="${esc(it.name)}" required></td>
    <td><input class="i-price" type="number" step="0.01" min="0.01" value="${esc(it.price || "")}" required></td>
    <td><input class="i-unit" value="${esc(it.unit || "each")}"></td>
    <td><input class="i-cap" type="number" min="1" value="${esc(it.daily_capacity || "")}"></td>
    <td><input class="i-all" value="${esc((it.allergens || []).join(", "))}"></td>
    <td><button type="button" class="btn small danger rm">×</button></td>`;
  tr.dataset.description = it.description || "";
  tr.dataset.tags = JSON.stringify(it.tags || []);
  $("#items-table tbody").appendChild(tr);
}
function windowRow(w = { day: "sat", start: "09:00", end: "12:00" }) {
  const tr = document.createElement("tr");
  tr.innerHTML = `<td><select class="w-day">${DAYS.map((d) => `<option ${d === w.day ? "selected" : ""}>${d}</option>`).join("")}</select></td>
    <td><input class="w-start" type="time" value="${esc(w.start)}"></td>
    <td><input class="w-end" type="time" value="${esc(w.end)}"></td>
    <td><button type="button" class="btn small danger rm">×</button></td>`;
  $("#windows-table tbody").appendChild(tr);
}
document.addEventListener("click", (e) => { if (e.target.classList.contains("rm")) e.target.closest("tr").remove(); });
$("#add-item").onclick = () => itemRow();
$("#add-window").onclick = () => windowRow();

extractForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = extractForm.querySelector("button"), status = $("#extract-status");
  btn.disabled = true; status.textContent = "Reading your menu...";
  try {
    const res = await fetch("/api/catalog/extract", { method: "POST", body: new FormData(extractForm) });
    const data = await res.json();
    if (!res.ok) throw new Error(data.message || "Could not read the menu");
    $("#items-table tbody").innerHTML = ""; $("#windows-table tbody").innerHTML = "";
    (data.items || []).forEach(itemRow);
    if (!data.items || !data.items.length) itemRow();
    (data.pickup_windows && data.pickup_windows.length ? data.pickup_windows : [undefined]).forEach((w) => windowRow(w));
    for (const k of ["name", "category", "description"]) if (data[k]) stallForm.elements[k].value = data[k];
    if (data.lead_time_hours != null) stallForm.elements.lead_time_hours.value = data.lead_time_hours;
    const q = data.questions_for_seller || [];
    $("#questions").innerHTML = q.length ? "<strong>Please check:</strong><ul>" + q.map((x) => `<li>${esc(x)}</li>`).join("") + "</ul>" : "";
    $("#questions").classList.toggle("hidden", !q.length);
    stallForm.classList.remove("hidden");
    status.textContent = `Found ${(data.items || []).length} items (${data.source === "claude" ? "read by Claude" : "basic text parser"}).`;
    stallForm.scrollIntoView({ behavior: "smooth" });
  } catch (err) { status.textContent = err.message; }
  finally { btn.disabled = false; }
});

stallForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = stallForm.elements, status = $("#publish-status");
  const items = [...document.querySelectorAll("#items-table tbody tr")].map((tr) => ({
    name: $(".i-name", tr).value.trim(), price: parseFloat($(".i-price", tr).value), unit: $(".i-unit", tr).value.trim() || "each",
    daily_capacity: $(".i-cap", tr).value ? parseInt($(".i-cap", tr).value, 10) : null,
    allergens: $(".i-all", tr).value.split(",").map((s) => s.trim()).filter(Boolean),
    description: tr.dataset.description, tags: JSON.parse(tr.dataset.tags || "[]"),
  })).filter((i) => i.name);
  const pickup_windows = [...document.querySelectorAll("#windows-table tbody tr")].map((tr) => ({
    day: $(".w-day", tr).value, start: $(".w-start", tr).value, end: $(".w-end", tr).value,
  }));
  const body = {
    name: f.name.value, category: f.category.value, description: f.description.value, city: f.city.value,
    pickup_address: f.pickup_address.value, paypal_email: f.paypal_email.value, ships: f.ships.checked,
    lead_time_hours: parseInt(f.lead_time_hours.value || "24", 10), items, pickup_windows,
    lat: 42.6334, lng: -71.3162,
  };
  status.textContent = "Publishing...";
  const res = await fetch("/api/stalls", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await res.json();
  if (!res.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map((d) => `${d.loc.slice(1).join(".")}: ${d.msg}`).join("; ") : data.message;
    status.textContent = "Could not publish: " + detail; return;
  }
  stallForm.classList.add("hidden"); extractForm.classList.add("hidden");
  $("#published").classList.remove("hidden");
  $("#published").innerHTML = `<h2>Your stall is live for AI agents</h2>
    <p>Public page: <a href="${data.public_url}">${data.public_url}</a></p>
    <p><strong>Private dashboard (bookmark this):</strong> <a href="${data.dashboard_url}">${data.dashboard_url}</a></p>
    <p>Agents find you through the MCP server:</p><pre><code>${data.mcp_url}</code></pre>
    <a class="btn primary" href="/shop">Try ordering from it with the buyer agent</a>`;
});
