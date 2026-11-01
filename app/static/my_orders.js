(() => {
  const list = document.getElementById("orders"), empty = document.getElementById("empty");
  async function show(id, prepend) {
    if (document.querySelector(`[data-order-id="${id}"]`)) return true;
    const res = await fetch(`/api/orders/${encodeURIComponent(id)}`);
    if (!res.ok) return false;
    const el = OpenStallCheckout.card(await res.json());
    prepend ? list.prepend(el) : list.appendChild(el);
    OpenStallCheckout.track(id, el);
    return true;
  }
  (async () => {
    const ids = OpenStallCheckout.remembered();
    for (const id of ids) await show(id);
    empty.classList.toggle("hidden", list.children.length > 0);
  })();
  document.getElementById("track-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const id = document.getElementById("track-id").value.trim().toUpperCase();
    if (!id) return;
    if (await show(id, true)) { OpenStallCheckout.remember(id); empty.classList.add("hidden"); }
    else alert("No order found with that number.");
  });
})();
