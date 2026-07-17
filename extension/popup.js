const $ = (selector) => document.querySelector(selector);
const ext = globalThis.browser ?? globalThis.chrome;
const localConfig = globalThis.RESUME_TAILOR_LOCAL ?? {};
const state = { jobText: "", analysis: null, edits: [] };

function show(id) { $(id).classList.remove("hidden"); }
function hide(id) { $(id).classList.add("hidden"); }
function setProgress(message) { $("#progress-text").textContent = message; show("#progress"); }
function showError(message) { $("#error").textContent = message; show("#error"); hide("#progress"); }
function clearError() { hide("#error"); $("#error").textContent = ""; }

async function settings() {
  const stored = await ext.storage.local.get(["backendUrl", "sharedSecret"]);
  return {
    backendUrl: (stored.backendUrl || "http://127.0.0.1:8765").replace(/\/$/, ""),
    sharedSecret: stored.sharedSecret || localConfig.sharedSecret || ""
  };
}

async function api(path, body) {
  const config = await settings();
  const url = new URL(config.backendUrl);
  if (url.protocol !== "http:" || !["127.0.0.1", "localhost"].includes(url.hostname)) {
    throw new Error("Backend URL must be a local http://127.0.0.1 or http://localhost address.");
  }
  const response = await fetch(`${config.backendUrl}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Extension-Secret": config.sharedSecret },
    body: JSON.stringify(body)
  });
  if (!response.ok) {
    let detail = `Backend returned ${response.status}`;
    try {
      const payload = await response.json();
      detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail, null, 2);
    } catch {}
    throw new Error(detail);
  }
  return response.json();
}

async function extractActivePage() {
  const [tab] = await ext.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id || !/^https?:/.test(tab.url || "")) throw new Error("Open a regular http(s) job posting tab first.");
  await ext.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
  const result = await ext.tabs.sendMessage(tab.id, { type: "EXTRACT_JOB_POSTING" });
  if (!result?.ok) throw new Error(result?.error || "Could not read this page.");
  return result.text;
}

function renderResults() {
  $("#company").textContent = state.analysis.company;
  $("#role").textContent = state.analysis.role;
  $("#keyword-count").textContent = state.analysis.keywords.length;
  $("#keywords").replaceChildren(...state.analysis.keywords.map((keyword) => {
    const chip = document.createElement("span"); chip.className = "chip"; chip.textContent = keyword.term; return chip;
  }));

  $("#edits").replaceChildren(...state.edits.map((edit, index) => {
    const card = document.createElement("article");
    card.className = `edit${edit.traceable ? "" : " flagged"}`;
    const head = document.createElement("div"); head.className = "edit-head";
    const check = document.createElement("input"); check.type = "checkbox"; check.dataset.index = index;
    check.checked = edit.traceable; check.disabled = !edit.traceable;
    const title = document.createElement("div"); title.className = "target";
    title.textContent = `${edit.target.section} · ${edit.target.anchor}${edit.target.item_index === null ? "" : ` · bullet ${edit.target.item_index + 1}`}`;
    head.append(check, title);
    const reason = document.createElement("p"); reason.className = "reason"; reason.textContent = edit.reason;
    const oldCopy = document.createElement("div"); oldCopy.className = "copy old"; oldCopy.textContent = edit.original_text || "Target not found";
    const newCopy = document.createElement("div"); newCopy.className = "copy new"; newCopy.textContent = edit.new_text;
    card.append(head, reason, oldCopy, newCopy);
    if (!edit.traceable) {
      const flag = document.createElement("div"); flag.className = "flag"; flag.textContent = edit.issues.join(" · "); card.append(flag);
    }
    return card;
  }));
  hide("#progress"); show("#results");
}

async function tailor() {
  clearError(); hide("#intro"); hide("#results"); hide("#done");
  try {
    setProgress("Reading visible job-posting text…");
    state.jobText = await extractActivePage();
    setProgress("Extracting role requirements…");
    state.analysis = await api("/extract-keywords", { job_text: state.jobText });
    setProgress("Drafting grounded resume edits…");
    const diff = await api("/generate-diff", { job_text: state.jobText, keywords: state.analysis.keywords });
    state.edits = diff.edits;
    renderResults();
  } catch (error) {
    showError(error.message); show("#intro");
  }
}

async function compile() {
  clearError();
  const approved = [...document.querySelectorAll(".edit input:checked")].map((input) => {
    const { original_text, traceable, issues, ...proposal } = state.edits[Number(input.dataset.index)];
    return proposal;
  });
  hide("#results"); setProgress("Compiling the selected edits…");
  try {
    const config = await settings();
    const result = await ext.runtime.sendMessage({
      type: "COMPILE_AND_DOWNLOAD",
      backendUrl: config.backendUrl,
      sharedSecret: config.sharedSecret,
      payload: {
        company: state.analysis.company,
        role: state.analysis.role,
        keywords: state.analysis.keywords,
        approved_edits: approved
      }
    });
    if (!result?.ok) throw new Error(result?.error || "The background download failed.");
    hide("#progress"); show("#done");
  } catch (error) {
    showError(error.message); show("#results");
  }
}

$("#tailor").addEventListener("click", tailor);
$("#compile").addEventListener("click", compile);
$("#restart").addEventListener("click", () => { hide("#done"); show("#intro"); });
$("#settings-toggle").addEventListener("click", () => $("#settings").classList.toggle("hidden"));
$("#save-settings").addEventListener("click", async () => {
  clearError();
  try {
    const url = new URL($("#backend-url").value.trim());
    if (url.protocol !== "http:" || !["127.0.0.1", "localhost"].includes(url.hostname)) throw new Error("Use a local HTTP backend URL.");
    await ext.storage.local.set({ backendUrl: url.href.replace(/\/$/, ""), sharedSecret: $("#shared-secret").value });
    $("#settings-note").textContent = "Saved.";
  } catch (error) { showError(error.message); }
});

settings().then((config) => { $("#backend-url").value = config.backendUrl; $("#shared-secret").value = config.sharedSecret; });
