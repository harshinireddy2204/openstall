const msgs = document.getElementById("messages"), trace = document.getElementById("trace");
const form = document.getElementById("chat-form"), input = document.getElementById("chat-input");
let sessionId = null;
const esc = (v) => String(v ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function add(role, text, approvalUrl) {
  const div = document.createElement("div");
  div.className = "msg " + role;
  div.textContent = text;
  if (approvalUrl) {
    const a = document.createElement("a");
    a.href = approvalUrl; a.className = "btn primary"; a.textContent = "Approve payment in PayPal";
    div.appendChild(document.createElement("br")); div.appendChild(a);
  }
  msgs.appendChild(div); msgs.scrollTop = msgs.scrollHeight;
  return div;
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = input.value.trim(); if (!text) return;
  add("user", text); input.value = "";
  const thinking = add("bot", "Working on it...");
  form.querySelector("button").disabled = true;
  try {
    const res = await fetch("/api/agent/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message: text, session_id: sessionId }) });
    const d = await res.json();
    thinking.remove();
    if (!res.ok) { add("bot", "Something went wrong. Please try again."); return; }
    sessionId = d.session_id;
    for (const s of d.steps || []) {
      const li = document.createElement("li");
      li.innerHTML = `<span class="fn">${esc(s.tool)}</span><br><span class="${s.ok ? "" : "blocked"}">${esc(s.summary)}</span>`;
      trace.appendChild(li);
    }
    add("bot", d.reply || "(no reply)", d.paypal_approval_url);
  } catch (err) { thinking.textContent = "Network error, please retry."; }
  finally { form.querySelector("button").disabled = false; input.focus(); }
});
