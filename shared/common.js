// Helpers shared by both portals: API calls, login, formatting, toasts, bill viewer.
const API_BASE = `${location.protocol}//${location.hostname}:5000/api`;

const Session = {
  key: `reimb-session-${location.port}`,
  get() {
    try { return JSON.parse(localStorage.getItem(this.key)); } catch { return null; }
  },
  set(value) { localStorage.setItem(this.key, JSON.stringify(value)); },
  clear() { localStorage.removeItem(this.key); },
  token() { return this.get()?.token || ""; },
};

class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

async function api(path, { method = "GET", body, form } = {}) {
  const headers = {};
  const token = Session.token();
  if (token) headers.Authorization = `Bearer ${token}`;
  let payload;
  if (form) payload = form;
  else if (body !== undefined) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }

  let resp;
  try {
    resp = await fetch(API_BASE + path, { method, headers, body: payload });
  } catch {
    throw new ApiError("Can't reach the backend server. Is it running (start.bat)?", 0);
  }
  let data = {};
  try { data = await resp.json(); } catch { /* empty body */ }
  if (resp.status === 401 && path !== "/login") {
    Session.clear();
    location.reload();
  }
  if (!resp.ok) throw new ApiError(data.error || `Request failed (${resp.status})`, resp.status);
  return data;
}

function fileUrl(billId) {
  return `${API_BASE}/bills/${billId}/file?token=${encodeURIComponent(Session.token())}`;
}

// ---------- Formatting ----------
const inr = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 2 });
const fmtMoney = (n) => inr.format(Number(n) || 0);

function fmtDate(iso) {
  if (!iso) return "-";
  const d = new Date(iso.length === 10 ? iso + "T00:00:00" : iso);
  if (isNaN(d)) return iso;
  return d.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });
}

function fmtDateTime(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString("en-IN", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

const todayIso = () => {
  const d = new Date();
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
};

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const STATUS_LABEL = {
  processing: "Reading bill...",
  draft: "Not sent",
  pending: "Pending with HR",
  approved: "Approved",
  rejected: "Rejected",
};

function statusBadge(status) {
  const spin = status === "processing" ? '<span class="spinner"></span> ' : "";
  return `<span class="badge ${status}">${spin}${STATUS_LABEL[status] || status}</span>`;
}

// ---------- Toasts ----------
function toast(message, kind = "") {
  let box = document.getElementById("toasts");
  if (!box) {
    box = document.createElement("div");
    box.id = "toasts";
    document.body.appendChild(box);
  }
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  box.appendChild(el);
  setTimeout(() => el.remove(), kind === "error" ? 6000 : 3500);
}

// ---------- Modals ----------
function openModal(html, { small = false } = {}) {
  const back = document.createElement("div");
  back.className = "modal-back";
  back.innerHTML = `<div class="modal ${small ? "small" : ""}">${html}</div>`;
  const close = () => { back.remove(); document.removeEventListener("keydown", onKey); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  back.addEventListener("click", (e) => { if (e.target === back || e.target.closest("[data-close]")) close(); });
  document.addEventListener("keydown", onKey);
  document.body.appendChild(back);
  return { root: back, close };
}

function viewBill(bill, { details = "", footer = "" } = {}) {
  const url = fileUrl(bill.id);
  const content = bill.file_type === "pdf"
    ? `<iframe src="${url}" title="Bill PDF"></iframe>`
    : `<img src="${url}" alt="Bill image">`;
  return openModal(`
    <div class="modal-head">
      <h3>${esc(bill.original_name)}</h3>
      <a class="btn btn-ghost" href="${url}" target="_blank" rel="noopener">Open in new tab</a>
      <button class="btn btn-ghost" data-close>Close</button>
    </div>
    <div class="modal-body">${details}${content}</div>
    ${footer ? `<div class="modal-foot">${footer}</div>` : ""}
  `);
}

function thumbHtml(bill) {
  if (bill.file_type === "pdf") return `<div class="thumb" data-view="${bill.id}" title="View bill">PDF</div>`;
  return `<img class="thumb" data-view="${bill.id}" src="${fileUrl(bill.id)}" alt="" loading="lazy" title="View bill">`;
}

// ---------- Login screen ----------
function setupLogin({ portal, onLoggedIn }) {
  const form = document.getElementById("login-form");
  const err = document.getElementById("login-error");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    err.textContent = "";
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true;
    try {
      const data = await api("/login", {
        method: "POST",
        body: { username: form.username.value, password: form.password.value, portal },
      });
      Session.set(data);
      form.reset();
      onLoggedIn(data.user);
    } catch (ex) {
      err.textContent = ex.message;
    } finally {
      btn.disabled = false;
    }
  });

  document.getElementById("logout-btn").addEventListener("click", async () => {
    try { await api("/logout", { method: "POST" }); } catch { /* ignore */ }
    Session.clear();
    location.reload();
  });
}

function showApp(user) {
  document.getElementById("login-screen").classList.add("hidden");
  document.getElementById("app-screen").classList.remove("hidden");
  document.getElementById("who-name").textContent = user.name;
  document.getElementById("who-meta").textContent = `${user.employee_code} · ${user.department}`;
}
