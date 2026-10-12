const BILL_TYPES = ["Fuel", "Repair", "Maintenance", "Other"];
const POLL_FAST = 2000;
const POLL_SLOW = 8000;

let bills = [];
let filter = "all";
let pollTimer = null;
let lastStatuses = null;          // id -> status, to announce HR decisions
const saving = new Set();         // bill ids with an edit in flight
const sending = new Set();

const rowsEl = document.getElementById("rows");
const emptyEl = document.getElementById("empty");
const sendAllBtn = document.getElementById("send-all");

// ---------- Boot ----------
setupLogin({ portal: "employee", onLoggedIn: start });

(async () => {
  if (!Session.token()) return;
  try {
    const data = await api("/me");
    start(data.user, data.extraction);
  } catch { /* api() handles 401 */ }
})();

async function start(user, extraction) {
  showApp(user);
  if (!extraction) {
    try { extraction = (await api("/me")).extraction; } catch { /* ignore */ }
  }
  showReaderBanner(extraction);
  await refresh();
}

function showReaderBanner(ex) {
  const el = document.getElementById("reader-banner");
  if (!ex || ex.ai) return el.classList.add("hidden");
  el.classList.remove("hidden");
  if (ex.ocr) {
    el.className = "banner info";
    el.textContent = "Bills are read with local OCR. Please check the bill number, date and amount before sending.";
  } else {
    el.className = "banner warn";
    el.textContent = "Automatic bill reading isn't set up on the server, so please type the bill number, date and amount yourself.";
  }
}

// ---------- Data ----------
async function refresh() {
  clearTimeout(pollTimer);
  try {
    const data = await api("/my/bills");
    announceDecisions(data.bills);
    bills = data.bills;
    render();
  } catch (ex) {
    if (ex.status !== 401) toast(ex.message, "error");
  }
  const busy = bills.some((b) => b.status === "processing");
  pollTimer = setTimeout(refresh, busy ? POLL_FAST : POLL_SLOW);
}

function announceDecisions(next) {
  if (lastStatuses) {
    for (const b of next) {
      const before = lastStatuses.get(b.id);
      if (before === "pending" && b.status === "approved") toast(`HR approved bill ${b.bill_number} (${fmtMoney(b.amount)})`, "ok");
      if (before === "pending" && b.status === "rejected") toast(`HR rejected bill ${b.bill_number}`, "error");
    }
  }
  lastStatuses = new Map(next.map((b) => [b.id, b.status]));
}

// ---------- Render ----------
function render() {
  const counts = { draft: 0, pending: 0, approved: 0, rejected: 0 };
  const amounts = { pending: 0, approved: 0 };
  for (const b of bills) {
    const key = b.status === "processing" ? "draft" : b.status;
    counts[key]++;
    if (key in amounts) amounts[key] += b.amount;
  }
  document.getElementById("st-draft").textContent = counts.draft;
  document.getElementById("st-pending").innerHTML = `${counts.pending}<small>${fmtMoney(amounts.pending)}</small>`;
  document.getElementById("st-approved").innerHTML = `${counts.approved}<small>${fmtMoney(amounts.approved)}</small>`;
  document.getElementById("st-rejected").textContent = counts.rejected;

  const visible = bills.filter((b) => filter === "all"
    || b.status === filter || (filter === "draft" && b.status === "processing"));

  // Don't wipe a field the user is typing in: re-render other rows only.
  const active = document.activeElement;
  const focused = active?.matches("input, select") ? active.closest("tr[data-id]") : null;
  const focusedId = focused ? Number(focused.dataset.id) : null;
  const existing = new Map([...rowsEl.children].map((tr) => [Number(tr.dataset.id), tr]));

  const frag = document.createDocumentFragment();
  for (const b of visible) {
    let tr = existing.get(b.id);
    const sig = rowSignature(b);
    if (!tr || (b.id !== focusedId && tr.dataset.sig !== sig)) {
      tr = document.createElement("tr");
      tr.dataset.id = b.id;
      tr.innerHTML = rowHtml(b);
    }
    tr.dataset.sig = sig;
    frag.appendChild(tr);
  }
  rowsEl.replaceChildren(frag);
  emptyEl.classList.toggle("hidden", visible.length > 0);
  emptyEl.textContent = bills.length ? "No bills with this status." : "No bills here yet. Upload your first bill above.";
  sendAllBtn.classList.toggle("hidden", !bills.some(isReady));
}

function rowSignature(b) {
  return [b.status, b.bill_number, b.bill_date, b.amount, b.bill_type, b.hr_comment, sending.has(b.id)].join("|");
}

function isReady(b) {
  return b.status === "draft" && b.bill_number && b.bill_date && b.amount > 0 && b.bill_type;
}

function rowHtml(b) {
  const fileCell = `
    <td><div class="file-cell">
      ${thumbHtml(b)}
      <div style="min-width:0">
        <div class="fname" title="${esc(b.original_name)}">${esc(b.original_name)}</div>
        <div class="fmeta">Uploaded ${fmtDateTime(b.uploaded_at)}</div>
      </div>
    </div></td>`;

  if (b.status === "processing") {
    return `${fileCell}<td colspan="4" class="muted"><span class="spinner"></span> AI is reading the bill...</td>
      <td>${statusBadge(b.status)}</td>
      <td><div class="actions"><button class="btn-icon" data-del="${b.id}" title="Delete">✕</button></div></td>`;
  }

  if (b.status === "draft") {
    const src = b.extraction_source === "ai" ? '<span class="src ai">AI</span>'
      : b.extraction_source === "ocr" ? '<span class="src">OCR</span>' : "";
    const note = b.extraction_note
      ? `<div class="note ${b.extraction_source === "ai" ? "" : "warn"}">${esc(b.extraction_note)}</div>` : "";
    const busy = sending.has(b.id);
    return `${fileCell}
      <td><select data-field="bill_type" class="${b.bill_type ? "" : "invalid"}">
        <option value="">Choose...</option>
        ${BILL_TYPES.map((t) => `<option ${t === b.bill_type ? "selected" : ""}>${t}</option>`).join("")}
      </select></td>
      <td><input type="text" data-field="bill_number" value="${esc(b.bill_number)}" maxlength="64"
           placeholder="Bill no." class="${b.bill_number ? "" : "invalid"}">${src}${note}</td>
      <td><input type="date" data-field="bill_date" value="${esc(b.bill_date)}" max="${todayIso()}"
           class="${b.bill_date ? "" : "invalid"}"></td>
      <td class="num"><input type="number" data-field="amount" value="${b.amount || ""}" min="0" step="0.01"
           placeholder="0.00" class="${b.amount > 0 ? "" : "invalid"}"></td>
      <td>${statusBadge(b.status)}</td>
      <td><div class="actions">
        <button class="btn btn-primary" data-send="${b.id}" ${busy ? "disabled" : ""}>${busy ? "Sending..." : "Send to HR"}</button>
        <button class="btn-icon" data-del="${b.id}" title="Delete">✕</button>
      </div></td>`;
  }

  const comment = b.hr_comment
    ? `<div class="comment ${b.status === "rejected" ? "rejected" : ""}">HR: ${esc(b.hr_comment)}</div>` : "";
  const when = b.decided_at ? `Decided ${fmtDateTime(b.decided_at)}` : `Sent ${fmtDateTime(b.submitted_at)}`;
  return `${fileCell}
    <td>${esc(b.bill_type)}</td>
    <td><strong>${esc(b.bill_number)}</strong></td>
    <td>${fmtDate(b.bill_date)}</td>
    <td class="num">${fmtMoney(b.amount)}</td>
    <td>${statusBadge(b.status)}${comment}<div class="note">${when}</div></td>
    <td></td>`;
}

// ---------- Upload ----------
const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("file-input");

dropzone.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); } });
dropzone.addEventListener("dragover", (e) => { e.preventDefault(); dropzone.classList.add("over"); });
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("over"));
dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropzone.classList.remove("over");
  upload([...e.dataTransfer.files]);
});
fileInput.addEventListener("change", () => { upload([...fileInput.files]); fileInput.value = ""; });

async function upload(files) {
  if (!files.length) return;
  const form = new FormData();
  files.forEach((f) => form.append("files", f));
  dropzone.querySelector(".big").innerHTML = `<span class="spinner"></span> Uploading ${files.length} file(s)...`;
  try {
    const data = await api("/my/bills", { method: "POST", form });
    toast(`${data.created.length} bill(s) uploaded. Reading them now...`, "ok");
    data.rejected.forEach((r) => toast(r, "error"));
    if (filter !== "all" && filter !== "draft") setFilter("all");
  } catch (ex) {
    toast(ex.message, "error");
  } finally {
    dropzone.querySelector(".big").textContent = "Drop bills here or click to choose files";
    refresh();
  }
}

// ---------- Editing ----------
rowsEl.addEventListener("change", async (e) => {
  const input = e.target.closest("[data-field]");
  if (!input) return;
  const id = Number(input.closest("tr").dataset.id);
  const field = input.dataset.field;
  const value = input.value;
  input.classList.toggle("invalid", !value || (field === "amount" && !(Number(value) > 0)));
  saving.add(id);
  try {
    const { bill } = await api(`/my/bills/${id}`, { method: "PATCH", body: { [field]: value } });
    replaceBill(bill, { rerender: false });
    input.closest("tr").dataset.sig = rowSignature(bill);
    sendAllBtn.classList.toggle("hidden", !bills.some(isReady));
  } catch (ex) {
    toast(ex.message, "error");
    input.classList.add("invalid");
  } finally {
    saving.delete(id);
  }
});

function replaceBill(bill, { rerender = true } = {}) {
  const i = bills.findIndex((b) => b.id === bill.id);
  if (i >= 0) bills[i] = bill;
  if (rerender) render();
}

function readRow(id) {
  const tr = rowsEl.querySelector(`tr[data-id="${id}"]`);
  const out = {};
  tr?.querySelectorAll("[data-field]").forEach((el) => { out[el.dataset.field] = el.value; });
  return out;
}

// ---------- Row actions ----------
rowsEl.addEventListener("click", async (e) => {
  const view = e.target.closest("[data-view]");
  if (view) return viewBill(bills.find((b) => b.id === Number(view.dataset.view)));

  const send = e.target.closest("[data-send]");
  if (send) return sendBill(Number(send.dataset.send));

  const del = e.target.closest("[data-del]");
  if (del) {
    const id = Number(del.dataset.del);
    const bill = bills.find((b) => b.id === id);
    if (!confirm(`Delete "${bill?.original_name}"? This can't be undone.`)) return;
    try {
      await api(`/my/bills/${id}`, { method: "DELETE" });
      bills = bills.filter((b) => b.id !== id);
      render();
    } catch (ex) { toast(ex.message, "error"); }
  }
});

async function sendBill(id, { quiet = false } = {}) {
  const fields = readRow(id);  // before render() rebuilds the row
  sending.add(id);
  render();
  try {
    const { bill } = await api(`/my/bills/${id}/send`, { method: "POST", body: fields });
    replaceBill(bill, { rerender: false });
    if (!quiet) toast(`Bill ${bill.bill_number} sent to HR`, "ok");
    return true;
  } catch (ex) {
    toast(ex.message, "error");
    return false;
  } finally {
    sending.delete(id);
    render();
  }
}

sendAllBtn.addEventListener("click", async () => {
  const ready = bills.filter(isReady);
  if (!ready.length) return;
  if (!confirm(`Send ${ready.length} bill(s) to HR?`)) return;
  sendAllBtn.disabled = true;
  let ok = 0;
  for (const b of ready) if (await sendBill(b.id, { quiet: true })) ok++;
  sendAllBtn.disabled = false;
  if (ok) toast(`${ok} bill(s) sent to HR`, "ok");
});

// ---------- Filters ----------
document.getElementById("tabs").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-filter]");
  if (btn) setFilter(btn.dataset.filter);
});

function setFilter(value) {
  filter = value;
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.filter === value));
  rowsEl.replaceChildren();
  render();
}
