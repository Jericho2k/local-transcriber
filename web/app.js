const API_BASE = "";
const POLL_INTERVAL_MS = 1800;
let token = null;
let state = null;
let selectedId = null;
let detail = null;
let explicitSelection = { pending: false };
let requestBusy = false;
let pollTimer = null;
let pollRunning = false;

const el = (id) => document.getElementById(id);
const text = (node, value) => {
  node.textContent = value;
};

function showError(message) {
  const notice = el("request-error");
  text(notice, message || "Request failed.");
  notice.classList.remove("hidden");
}

function clearError() {
  el("request-error").classList.add("hidden");
  text(el("request-error"), "");
}

function requestInfo(message) {
  const notice = el("request-status");
  if (!message) {
    notice.classList.add("hidden");
    text(notice, "");
    return;
  }
  text(notice, message);
  notice.classList.remove("hidden");
}

function badgeClass(status) {
  if (status === "running") return "status-running";
  if (status === "completed") return "status-completed";
  if (status === "failed") return "status-failed";
  if (status && status !== "idle") return "status-queued";
  return "status-neutral";
}

function loginMessage(status) {
  if (status === "ready") return "Ready";
  if (status === "starting") return "Starting";
  if (status === "error") return "Setup problem";
  return "Setup active";
}

async function parseResponse(response) {
  const raw = await response.text();
  let body = null;
  if (raw) {
    try {
      body = JSON.parse(raw);
    } catch {
      body = { error: raw.slice(0, 160) };
    }
  }
  return body || {};
}

async function api(path, options = {}) {
  if (!token) {
    throw new Error("Local token is missing. Reopen the supplied application URL.");
  }
  const headers = { "X-Local-Token": token };
  if (options.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(API_BASE + path, {
    ...options,
    headers: { ...headers, ...(options.headers || {}) },
    cache: "no-store",
  });
  if (!response.ok) {
    const body = await parseResponse(response);
    throw new Error(body.error || `Request failed with ${response.status}.`);
  }
  return parseResponse(response);
}

function extractToken() {
  const params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  const fromHash = params.get("token");
  if (fromHash) {
    sessionStorage.setItem("transcriber-token", fromHash);
    window.history.replaceState({}, document.title, window.location.pathname + window.location.search);
  }
  return sessionStorage.getItem("transcriber-token");
}

function statusLabel(value) {
  return String(value || "idle").replace(/_/g, " ");
}

function jobTitle(job) {
  return (job && (job.title || job.source || "Transcription job")) || "Transcription job";
}

function selectJob(id) {
  if (!id) return;
  selectedId = id;
  detail = null;
  renderHistory();
  renderJob();
  renderControls();
  const pendingSelection = id;
  explicitSelection.pending = true;
  refreshDetail().then((loaded) => {
    if (!loaded || pendingSelection !== selectedId) return;
    const job = loaded.job || loaded;
    if (job && (job.status === "stopped" || job.status === "failed")) {
      el("source").value = job.source || el("source").value;
      if (["auto", "en", "ru"].includes(job.language)) {
        el("language").value = job.language;
      }
      if (["auto", "cuda", "cpu"].includes(job.device)) {
        el("device").value = job.device;
      }
    }
    explicitSelection.pending = false;
    renderStartControl();
    renderControls();
  }).catch((error) => {
    explicitSelection.pending = false;
    showError(error.message || "Could not load selected job.");
  });
}

function chooseSelection(jobs) {
  const ids = new Set(jobs.map((job) => job.id));
  if (selectedId && ids.has(selectedId)) return;
  const active = state && state.active_id;
  if (active && ids.has(active)) selectedId = active;
  else if (jobs.length) selectedId = jobs[0].id;
  else selectedId = null;
}

async function refreshState() {
  const next = await api("/api/state");
  state = next;
  const jobs = Array.isArray(next.jobs) ? next.jobs : [];
  chooseSelection(jobs);
  renderSetup(next.setup, state);
  renderHistory();
  renderStartControl();
  renderControls();
  renderActiveSummary();
  await refreshDetail();
  renderControls();
}

async function refreshDetail() {
  const requestedId = selectedId;
  if (!requestedId) {
    detail = null;
    renderJob();
    return null;
  }
  const exists = Array.isArray(state?.jobs) && state.jobs.some((job) => job.id === requestedId);
  if (!exists) {
    detail = null;
    renderJob();
    return null;
  }
  const response = await api(`/api/jobs/${encodeURIComponent(requestedId)}`);
  if (requestedId !== selectedId) return null;
  detail = response;
  renderJob();
  return detail;
}

function renderSetup(setup) {
  const pill = el("setup-status");
  const ready = Boolean(setup && setup.ready);
  text(pill, ready ? "Ready" : "Setup active");
  pill.className = `status-pill ${ready ? "status-completed" : "status-queued"}`;
  if (setup && setup.message) pill.title = setup.message;
}

function renderHistory() {
  const list = el("history-list");
  const previousFocus = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.jobId : null;
  while (list.firstChild) list.removeChild(list.firstChild);
  const jobs = Array.isArray(state?.jobs) ? state.jobs : [];
  if (jobs.length === 0) {
    const item = document.createElement("li");
    text(item, "No transcription jobs yet.");
    item.className = "muted";
    list.appendChild(item);
    el("history-meta").textContent = "No jobs";
    return;
  }
  text(el("history-meta"), `${jobs.length} job${jobs.length === 1 ? "" : "s"} stored locally`);
  for (const job of jobs) {
    const item = document.createElement("li");
    item.className = "history-item";
    item.dataset.jobId = job.id;
    if (job.id === selectedId) item.classList.add("active");
    item.tabIndex = 0;
    item.setAttribute("role", "button");
    const title = document.createElement("p");
    title.className = "history-title";
    text(title, jobTitle(job));
    const meta = document.createElement("div");
    meta.className = "history-meta";
    const status = document.createElement("span");
    text(status, statusLabel(job.status));
    const progress = document.createElement("span");
    text(progress, `${Math.round(Number(job.progress) || 0)}%`);
    meta.appendChild(status);
    meta.appendChild(progress);
    item.appendChild(title);
    item.appendChild(meta);
    if (job.error) {
      const error = document.createElement("span");
      error.className = "muted";
      text(error, job.error);
      item.appendChild(error);
    }
    const open = () => selectJob(job.id);
    item.addEventListener("click", open);
    item.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        open();
      }
    });
    list.appendChild(item);
  }
   if (previousFocus) {
     const row = Array.from(list.children).find((node) => node.dataset && node.dataset.jobId === previousFocus);
     if (row) row.focus();
   }
 }

 function renderStartControl() {
 const jobs = Array.isArray(state?.jobs) ? state.jobs : [];
 const selectedJob = jobs.find((job) => job.id === selectedId);
 const button = el("start-button");
 let matching = false;
 if (selectedJob && (selectedJob.status === "stopped" || selectedJob.status === "failed")) {
   matching = el("source").value.trim() === (selectedJob.source || "")
     && el("language").value === (selectedJob.language || "auto");
 }
 const label = matching ? "Resume transcription" : "Start transcription";
 button.textContent = label;
 }

function renderControls() {
  renderStartControl();
  const ready = state && state.setup ? Boolean(state.setup.ready) : true;
  const active = state ? state.active_id : null;
  const activeJob = state && Array.isArray(state.jobs)
    ? state.jobs.find((item) => item.id === active)
    : null;
  const start = el("start-button");
  const choose = el("choose-file");
  const stop = el("stop-button");
  const open = el("open-folder");
  if (start) start.disabled = requestBusy || !ready || Boolean(active);
  if (choose) choose.disabled = requestBusy;
  if (stop) {
    stop.disabled = requestBusy || !activeJob || !["running", "queued"].includes(activeJob.status);
  }
  renderStopControl(activeJob);
  if (open) open.disabled = requestBusy || !detail || !detail.output_dir;
}

function renderActiveSummary() {
  const active = state && state.active_id;
  const textNode = el("active-summary");
  if (active) {
    const job = (state.jobs || []).find((item) => item.id === active);
    text(textNode, job ? `Active: ${jobTitle(job)} (${statusLabel(job.status)})` : "Active job in progress");
  } else {
    text(textNode, "No active job");
  }
}

function statusPill(node, value) {
  text(node, statusLabel(value));
  node.className = `status-pill ${badgeClass(value)}`;
}

function renderJob() {
  const job = detail ? detail.job || detail : null;
  if (!job) {
    text(el("job-title"), "No job selected");
    statusPill(el("job-status-pill"), "idle");
    text(el("progress-text"), "0%");
    el("progress-bar").value = 0;
    text(el("progress-detail"), "No job details loaded yet.");
    ["job-source", "job-language", "job-created", "job-device", "preview"].forEach((id) => {
      const target = el(id);
      if (target) text(target, id === "preview" ? "No transcript is available for the selected job yet." : "—");
    });
    toggleExportButtons(false);
    renderLogs([]);
    maybeShowJobError(null);
    return;
  }
  text(el("job-title"), jobTitle(job));
  statusPill(el("job-status-pill"), job.status);
  const progress = Math.max(0, Math.min(100, Math.round(Number(job.progress) || 0)));
  el("progress-bar").value = progress;
  text(el("progress-text"), `${progress}%`);
  const completed = Number(job.completed_parts) || 0;
  const total = Number(job.total_parts) || 0;
  if (job.status === "failed") {
    text(el("progress-detail"), (job.error ? `Failed: ${job.error}` : "Transcription stopped with an error."));
  } else if (total > 0) {
    text(el("progress-detail"), `${completed} of ${total} parts processed locally.`);
  } else {
    text(el("progress-detail"), "Preparing job details…");
  }
  text(el("job-source"), job.source || "—");
  text(el("job-language"), statusLabel(job.language || "auto"));
  text(el("job-created"), formatTime(job.created_at));
  text(el("job-device"), statusLabel(job.device || "auto"));
  text(el("preview"), detail.preview ? detail.preview : "No transcript preview is available yet.");
  renderLogs(Array.isArray(detail.logs) ? detail.logs : []);
  maybeShowJobError(job.status === "failed" ? job.error : null);
  const isActive = Boolean(state && state.active_id) && state.active_id !== job.id;
  toggleExportButtons(true, detail, job);
  renderStopControl(job);
  if (isActive) {
    text(el("active-summary"), `Another job is active: ${activeName()}`);
  }
}

function formatTime(value) {
  if (!value) return "—";
  const numeric = Number(value);
  const milliseconds = Number.isFinite(numeric) && numeric < 1e12 ? numeric * 1000 : numeric;
  const date = new Date(milliseconds);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString();
}

function activeName() {
  if (!state || !state.active_id) return "";
  const job = (state.jobs || []).find((item) => item.id === state.active_id);
  return job ? jobTitle(job) : state.active_id;
}

function renderStopControl(job) {
  const button = el("stop-button");
  const buttonEnabled = Boolean(job) && (job.status === "running" || job.status === "queued" || job.status === "stopping");
  button.disabled = !buttonEnabled || requestBusy;
}

function renderLogs(logs) {
  const list = el("log-lines");
  const empty = el("logs-empty");
  while (list.firstChild) list.removeChild(list.firstChild);
  if (!Array.isArray(logs) || logs.length === 0) {
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");
  for (const entry of logs) {
    const item = document.createElement("li");
    text(item, String(entry));
    list.appendChild(item);
  }
}

function maybeShowJobError(message) {
  const node = el("job-error");
  if (!message) {
    node.classList.add("hidden");
    text(node, "");
    return;
  }
  text(node, message);
  node.classList.remove("hidden");
}

function toggleExportButtons(enabled, nextDetail = detail, job = detail?.job || null) {
  const buttons = document.querySelectorAll("[data-format]");
  const files = nextDetail && Array.isArray(nextDetail.files) ? nextDetail.files : [];
  const currentJob = job || (nextDetail ? nextDetail.job : null);
  const canUse = enabled && Boolean(currentJob);
  for (const button of buttons) {
    const format = button.getAttribute("data-format");
    const file = files.find((item) => item.format === format);
    button.disabled = !(canUse && file);
  }
  el("open-folder").disabled = !(canUse && nextDetail && nextDetail.output_dir);
}

async function saveTokenFlow() {
  if (!token) {
    const fatal = el("fatal-error");
    text(fatal, "Missing local authentication. Reopen START.cmd to get a fresh local browser address.");
    fatal.classList.remove("hidden");
    for (const button of document.querySelectorAll("button, input, select")) {
      button.disabled = true;
    }
    return false;
  }
  return true;
}

async function submitJob(event) {
  event.preventDefault();
  clearError();
  const source = el("source").value.trim();
  if (!source) {
    showError("Enter a local file path or a YouTube URL.");
    return;
  }
  const payload = {
    source,
    language: el("language").value,
    device: el("device").value,
  };
  requestBusy = true;
  updateBusyControls(true);
  requestInfo("Starting or resuming a local transcription job…");
  try {
    const response = await api("/api/jobs", { method: "POST", body: JSON.stringify(payload) });
    if (response && response.job) {
      selectedId = response.job.id;
      detail = null;
    }
    await refreshState();
    clearError();
    requestInfo("Job queued or reused on the backend. Progress is polled automatically.");
    window.setTimeout(() => requestInfo(""), 2500);
  } catch (error) {
    requestInfo("");
    showError(error.message || "Could not start the transcription job.");
  } finally {
    requestBusy = false;
    updateBusyControls(false);
  }
}

async function chooseFile() {
  clearError();
  requestBusy = true;
  updateBusyControls(true);
  requestInfo("Opening native file picker…");
  try {
    const response = await api("/api/select-file", { method: "POST", body: JSON.stringify({}) });
    if (response.path) {
      el("source").value = response.path;
      clearError();
    }
  } catch (error) {
    showError(error.message || "File selection failed.");
  } finally {
    requestBusy = false;
    requestInfo("");
    updateBusyControls(false);
  }
}

async function stopJob() {
  if (!selectedId || requestBusy) return;
  requestBusy = true;
  updateBusyControls(true);
  requestInfo("Stopping. Completed blocks are preserved; this may take a moment.");
  try {
    await api("/api/stop", { method: "POST", body: JSON.stringify({}) });
    clearError();
    await refreshState();
  } catch (error) {
    showError(error.message || "Could not stop the job.");
  } finally {
    requestBusy = false;
    requestInfo("");
    updateBusyControls(false);
  }
}

async function openFolder() {
  if (!selectedId) return;
  requestBusy = true;
  updateBusyControls(true);
  try {
    await api("/api/open-folder", { method: "POST", body: JSON.stringify({ id: selectedId }) });
    requestInfo("The job folder was requested from the local server.");
    window.setTimeout(() => requestInfo(""), 2000);
  } catch (error) {
    showError(error.message || "Could not open folder.");
  } finally {
    requestBusy = false;
    updateBusyControls(false);
  }
}

async function download(format) {
  clearError();
  if (!selectedId) return;
  try {
    const response = await fetch(
      `/api/download?id=${encodeURIComponent(selectedId)}&format=${encodeURIComponent(format)}`,
      { headers: { "X-Local-Token": token }, cache: "no-store" }
    );
    if (!response.ok) {
      const body = await parseResponse(response);
      throw new Error(body.error || `Download failed with ${response.status}.`);
    }
    const blob = await response.blob();
    const file = detail && Array.isArray(detail.files)
      ? detail.files.find((item) => item.format === format)
      : null;
    const filename = (file && file.name) || `transcript.${format === "json" ? "json" : "txt"}`;
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    anchor.rel = "noopener";
    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) {
    showError(error.message || "Download failed.");
  }
}

function updateBusyControls(isBusy) {
  const start = el("start-button");
  const choose = el("choose-file");
  const stop = el("stop-button");
  const open = el("open-folder");
  for (const button of [start, choose, open]) {
    if (button) button.toggleAttribute("aria-busy", Boolean(isBusy));
  }
  if (start) start.disabled = Boolean(isBusy) || (state && Boolean(state.active_id));
  if (choose) choose.disabled = Boolean(isBusy);
  if (open) open.disabled = Boolean(isBusy) || open.disabled;
  renderHistory();
  renderControls();
}

function wireEvents() {
  el("transcription-form").addEventListener("submit", submitJob);
  el("choose-file").addEventListener("click", chooseFile);
  el("stop-button").addEventListener("click", stopJob);
  el("open-folder").addEventListener("click", openFolder);
  el("refresh-button").addEventListener("click", () => {
    refreshState().catch((error) => showError(error.message));
  });
  el("clear-selection").addEventListener("click", () => {
    if (state && state.active_id) selectedId = state.active_id;
    else selectedId = null;
    detail = null;
    renderHistory();
    renderJob();
    renderControls();
    if (selectedId) refreshDetail().catch(() => {});
  });
  el("source").addEventListener("input", () => {
    renderStartControl();
    renderControls();
  });
  el("language").addEventListener("change", () => {
    renderStartControl();
    renderControls();
  });
  document.querySelectorAll("[data-format]").forEach((button) => {
    button.addEventListener("click", () => download(button.getAttribute("data-format")));
  });
}

async function pollOnce() {
  if (pollRunning || requestBusy) return;
  pollRunning = true;
  try {
    await refreshState();
  } catch (error) {
    showError(error.message || "Polling failed.");
  } finally {
    pollRunning = false;
  }
}

async function boot() {
  token = extractToken();
  if (!(await saveTokenFlow())) return;
  wireEvents();
  try {
    await refreshState();
  } catch (error) {
    showError(error.message || "Could not connect to the local server.");
  }
  pollTimer = window.setInterval(() => {
    pollOnce();
  }, POLL_INTERVAL_MS);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) pollOnce();
  });
}

window.addEventListener("DOMContentLoaded", () => {
  boot().catch((error) => {
    const notice = el("fatal-error");
    text(notice, error && error.message ? error.message : "The interface failed to load.");
    notice.classList.remove("hidden");
  });
});
