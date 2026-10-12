const POLL_MS = 5000;

let pending = [];
let history = [];
let historyFilter = "all";
let modeFilter = "all";
let query = "";
let onlyAnomalies = false;
let pollTimer = null;
const busy = new Set();

const pendingRows = document.getElementById("pending-rows");
const historyRows = document.getElementById("history-rows");

// ---------- Boot ----------
setupLogin({ portal: "hr", onLoggedIn: start });

(async () => {
  if (!Session.token()) return;
  try { start((await api("/me")).user); } catch { /* api() handles 401 */ }
})();

function start(user) {
  showApp(user);
  refresh();
}

// ---------- Data ----------
async function refresh() {
  clearTimeout(pollTimer);
  try {
    const data = await api(`/hr/bills${onlyAnomalies ? "?anomalies=1" : ""}`);
    pending = data.pending;
    history = data.history;
    renderStats(data.stats);
    renderDetection(data.detection);
    renderAuto(data.auto_approve, data.stats.auto_count);
    renderPending();
    renderHistory();
  } catch (ex) {
    if (ex.status !== 401) toast(ex.message, "error");
  }
  pollTimer = setTimeout(refresh, POLL_MS);
}

function renderStats(s) {
  document.getElementById("st-pending").innerHTML = `${s.pending_count}<small>${fmtMoney(s.pending_amount)}</small>`;
  document.getElementById("st-approved").innerHTML = `${s.approved_count}<small>${fmtMoney(s.approved_amount)}</small>`;
  document.getElementById("st-rejected").textContent = s.rejected_count;
  document.getElementById("st-anomaly").innerHTML = `${s.anomaly_pending}<small>${s.anomaly_total} flagged in total</small>`;
  document.getElementById("pending-count").textContent = s.pending_count;
}

function renderDetection(d) {
  const el = document.getElementById("detection-banner");
  if (!d) return el.classList.add("hidden");
  const ml = d.ml_active ? "statistical rules + Isolation Forest ML model"
    : d.ml_installed ? `statistical rules (the ML model starts once there are ${d.ml_min_bills} past bills)`
    : "statistical rules (ML model unavailable: scikit-learn isn't installed)";
  const sample = d.sample_bills ? `, including ${d.sample_bills} sample bills` : "";
  const range = (r) => !r ? "" : r.min === r.max ? fmtMoney(r.min) : `${fmtMoney(r.min)} to ${fmtMoney(r.max)}`;
  const lim = d.limits || {};
  const learned = lim.per_bill || lim.per_month
    ? ` Learned cut-offs: a single bill above ${range(lim.per_bill) || "n/a"}, a monthly total per bill type above ${range(lim.per_month) || "n/a"}.`
    : "";
  el.textContent = `Anomaly detection: ${ml}, learning from ${d.history_bills} past bills${sample}.${learned} Flagged bills are shown in red.`;
  el.classList.remove("hidden");
}

function renderAuto(a, autoCount) {
  const toggle = document.getElementById("auto-toggle");
  if (!busy.has("auto")) toggle.checked = a.enabled;
  document.getElementById("auto-card").classList.toggle("on", a.enabled);
  const status = document.getElementById("auto-status");
  status.textContent = a.enabled
    ? `ON since ${fmtDateTime(a.since)}. Bills sent from then on are approved automatically unless they're flagged as anomalous or look like a duplicate; those wait here for you. Auto-approved so far: ${autoCount}.`
    : `OFF. Every bill waits for your decision.${a.since ? ` Auto-approved so far: ${autoCount}.` : ""} When on, only bills sent after you switch it on are affected.`;
}

// ---------- Render ----------
function anomalyReasons(b) {
  if (!b.anomaly) return "";
  return `<span class="badge anom">⚠ Anomaly</span>
    <ul class="anom-reasons">${b.anomaly_reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>`;
}

function employeeCell(b) {
  return `<td><strong>${esc(b.employee_name)}</strong><div class="note">${esc(b.employee_code)} · ${esc(b.department)}</div></td>
    <td><div class="file-cell" style="min-width:0">${thumbHtml(b)}<button class="link" data-view="${b.id}">View</button></div></td>
    <td>${esc(b.bill_type)}</td>`;
}

function dupBadge(b) {
  return b.duplicate ? '<span class="badge dup" title="Another submitted bill has the same number and amount">Possible duplicate</span>' : "";
}

function renderPending() {
  pendingRows.innerHTML = pending.map((b) => {
    const dis = busy.has(b.id) ? "disabled" : "";
    return `<tr data-id="${b.id}" class="${b.anomaly ? "anomaly" : ""}">
      ${employeeCell(b)}
      <td><strong>${esc(b.bill_number)}</strong>${dupBadge(b)}</td>
      <td>${fmtDate(b.bill_date)}</td>
      <td class="num"><strong>${fmtMoney(b.amount)}</strong>${anomalyReasons(b)}</td>
      <td>${fmtDateTime(b.submitted_at)}</td>
      <td><div class="actions">
        <button class="btn btn-green" data-approve="${b.id}" ${dis}>Approve</button>
        <button class="btn btn-red" data-reject="${b.id}" ${dis}>Reject</button>
      </div></td>
    </tr>`;
  }).join("");
  const empty = document.getElementById("pending-empty");
  empty.classList.toggle("hidden", pending.length > 0);
  empty.textContent = onlyAnomalies ? "No anomalous bills are waiting." : "All caught up. No bills are waiting for review.";
}

function decidedBy(b) {
  if (b.decision_mode === "auto") return '<span class="badge auto">Auto-approved</span>';
  return `${esc(b.decided_by_name || "HR")}<div class="note">Manual</div>`;
}

function historyList() {
  const q = query.trim().toLowerCase();
  return history.filter((b) =>
    (historyFilter === "all" || b.status === historyFilter)
    && (modeFilter === "all" || b.decision_mode === modeFilter)
    && (!q || [b.employee_name, b.employee_code, b.department, b.bill_number, b.bill_type, b.decided_by_name, b.hr_comment]
      .some((v) => String(v || "").toLowerCase().includes(q))));
}

function renderHistory() {
  const list = historyList();
  historyRows.innerHTML = list.map((b) => {
    const comment = b.hr_comment
      ? `<div class="comment ${b.status === "rejected" ? "rejected" : ""}">${esc(b.hr_comment)}</div>` : "";
    return `<tr data-id="${b.id}" class="${b.anomaly ? "anomaly" : ""}">
      ${employeeCell(b)}
      <td><strong>${esc(b.bill_number)}</strong>${dupBadge(b)}</td>
      <td>${fmtDate(b.bill_date)}</td>
      <td class="num"><strong>${fmtMoney(b.amount)}</strong>${anomalyReasons(b)}</td>
      <td>${statusBadge(b.status)}${comment}</td>
      <td>${decidedBy(b)}</td>
      <td>${fmtDateTime(b.decided_at)}</td>
    </tr>`;
  }).join("");
  document.getElementById("history-count").textContent = list.length === history.length ? history.length : `${list.length} of ${history.length}`;
  const empty = document.getElementById("history-empty");
  empty.classList.toggle("hidden", list.length > 0);
  empty.textContent = history.length ? "No decisions match these filters." : "No decisions yet. Approved and rejected bills will stay listed here.";
}

// ---------- Auto-approve switch ----------
document.getElementById("auto-toggle").addEventListener("change", async (e) => {
  const enabled = e.target.checked;
  if (enabled && !confirm("Turn on auto-approve?\n\nFrom now on, bills that employees send and that are not flagged as anomalous will be approved automatically. Flagged bills still wait for you.")) {
    e.target.checked = false;
    return;
  }
  busy.add("auto");
  try {
    const { auto_approve } = await api("/hr/auto-approve", { method: "POST", body: { enabled } });
    toast(auto_approve.enabled ? "Auto-approve is ON" : "Auto-approve is OFF", auto_approve.enabled ? "ok" : "");
  } catch (ex) {
    toast(ex.message, "error");
    e.target.checked = !enabled;
  } finally {
    busy.delete("auto");
    refresh();
  }
});

// ---------- Actions ----------
document.addEventListener("click", (e) => {
  const view = e.target.closest("[data-view]");
  if (view) return openViewer(Number(view.dataset.view));
  const approve = e.target.closest("[data-approve]");
  if (approve) return decide(Number(approve.dataset.approve), "approve");
  const reject = e.target.closest("[data-reject]");
  if (reject) return askReject(Number(reject.dataset.reject));
});

function findBill(id) {
  return pending.find((x) => x.id === id) || history.find((x) => x.id === id);
}

function openViewer(id) {
  const b = findBill(id);
  if (!b) return;
  const anom = b.anomaly ? `<div class="anom-box"><strong>⚠ Anomalous amount</strong>
    <ul>${b.anomaly_reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul></div>` : "";
  const decision = b.status === "pending" ? ""
    : `<div><span>Decision</span>${b.decision_mode === "auto" ? "Auto-approved" : `${esc(b.status)} by ${esc(b.decided_by_name || "HR")}`}
        <small class="muted"> ${fmtDateTime(b.decided_at)}</small></div>`;
  const details = `${anom}<div class="details">
    <div><span>Employee</span>${esc(b.employee_name)} (${esc(b.employee_code)})</div>
    <div><span>Type</span>${esc(b.bill_type)}</div>
    <div><span>Bill number</span>${esc(b.bill_number)}</div>
    <div><span>Bill date</span>${fmtDate(b.bill_date)}</div>
    <div><span>Amount</span>${fmtMoney(b.amount)}</div>
    <div><span>Status</span>${statusBadge(b.status)}</div>
    ${decision}
    ${b.hr_comment ? `<div><span>Comment</span>${esc(b.hr_comment)}</div>` : ""}
  </div>`;
  const footer = b.status === "pending"
    ? `<button class="btn btn-red" data-m-reject>Reject</button><button class="btn btn-green" data-m-approve>Approve</button>` : "";
  const modal = viewBill(b, { details, footer });
  modal.root.querySelector("[data-m-approve]")?.addEventListener("click", async () => {
    if (await decide(id, "approve")) modal.close();
  });
  modal.root.querySelector("[data-m-reject]")?.addEventListener("click", () => { modal.close(); askReject(id); });
}

function askReject(id) {
  const b = findBill(id);
  const modal = openModal(`
    <div class="modal-head"><h3>Reject bill ${esc(b?.bill_number)}</h3></div>
    <div class="modal-body">
      <p style="margin-top:0">${esc(b?.employee_name)} · ${fmtMoney(b?.amount)}. The employee will see this reason.</p>
      <textarea id="reject-reason" maxlength="500" placeholder="e.g. Bill is not readable, please upload a clearer photo"></textarea>
      <div class="form-error" id="reject-error"></div>
    </div>
    <div class="modal-foot">
      <button class="btn btn-ghost" data-close>Cancel</button>
      <button class="btn btn-red" id="reject-confirm">Reject bill</button>
    </div>`, { small: true });
  const box = modal.root.querySelector("#reject-reason");
  box.focus();
  modal.root.querySelector("#reject-confirm").addEventListener("click", async () => {
    const reason = box.value.trim();
    if (!reason) {
      modal.root.querySelector("#reject-error").textContent = "Please enter a reason.";
      return;
    }
    if (await decide(id, "reject", reason)) modal.close();
  });
}

async function decide(id, action, comment = "") {
  busy.add(id);
  renderPending();
  try {
    const { bill } = await api(`/hr/bills/${id}/decision`, { method: "POST", body: { action, comment } });
    toast(`Bill ${bill.bill_number} ${bill.status}`, bill.status === "approved" ? "ok" : "");
    return true;
  } catch (ex) {
    toast(ex.message, "error");
    return false;
  } finally {
    busy.delete(id);
    refresh();
  }
}

// ---------- Filters ----------
document.getElementById("history-tabs").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-filter]");
  if (!btn) return;
  historyFilter = btn.dataset.filter;
  document.querySelectorAll("#history-tabs button").forEach((b) => b.classList.toggle("active", b === btn));
  renderHistory();
});

document.getElementById("mode-filter").addEventListener("change", (e) => { modeFilter = e.target.value; renderHistory(); });
document.getElementById("search").addEventListener("input", (e) => { query = e.target.value; renderHistory(); });
document.getElementById("only-anomalies").addEventListener("change", (e) => {
  onlyAnomalies = e.target.checked;
  refresh();
});
