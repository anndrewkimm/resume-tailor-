const $ = (selector) => document.querySelector(selector);
const ext = globalThis.browser ?? globalThis.chrome;
const localConfig = globalThis.RESUME_TAILOR_LOCAL ?? {};
const state = { analysis: null, edits: [], paragraphs: [] };

function show(id) { $(id).classList.remove("hidden"); }
function hide(id) { $(id).classList.add("hidden"); }
function hideViews() { ["#intro", "#progress", "#results", "#letter", "#done"].forEach(hide); }
function setProgress(message) { hideViews(); $("#progress-text").textContent = message; show("#progress"); }
function showError(message) { $("#error").textContent = message; show("#error"); hide("#progress"); }
function clearError() { hide("#error"); $("#error").textContent = ""; }

async function settings() {
  const stored = await ext.storage.local.get(["backendUrl", "sharedSecret"]);
  return {
    backendUrl: (stored.backendUrl || "http://127.0.0.1:8765").replace(/\/$/, ""),
    sharedSecret: stored.sharedSecret || localConfig.sharedSecret || ""
  };
}

async function activeTabId() {
  const [tab] = await ext.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id || !/^https?:/.test(tab.url || "")) throw new Error("Open a regular http(s) job posting tab first.");
  return tab.id;
}

function renderFitAndKeywords() {
  const fit = state.analysis.fit;
  const keywords = fit ? [...fit.matched, ...fit.missing] : state.analysis.keywords;
  $("#keyword-count").textContent = keywords.length;
  if (fit) {
    $("#fit-summary").textContent = `Fit: ${fit.score}% — ${fit.matched.length} of ${keywords.length} keywords covered`;
    show("#fit-summary");
    const hasHighMissing = fit.missing.some((keyword) => keyword.importance === "high");
    $("#fit-warning").textContent = hasHighMissing
      ? "Missing keywords cannot be added by tailoring — this tool never invents experience. A low fit score means this posting may not be worth the application, or the resume needs real (human) updating."
      : "";
    $("#fit-warning").classList.toggle("hidden", !hasHighMissing);
  } else {
    hide("#fit-summary");
    hide("#fit-warning");
  }
  $("#keywords").replaceChildren(...keywords.map((keyword) => {
    const chip = document.createElement("span");
    chip.className = `chip${keyword.matched === false ? " chip-missing" : ""}`;
    chip.textContent = keyword.term;
    return chip;
  }));
}

function commonRejectedEntitySummary() {
  const flaggedBullets = state.edits.filter((edit) =>
    !edit.traceable && ["Experience", "Projects"].includes(edit.target.section)
  );
  const counts = new Map();
  for (const edit of flaggedBullets) {
    const seen = new Set();
    for (const issue of edit.issues || []) {
      const match = issue.match(/^entity is not grounded in the original target bullet:\s*(.+)$/i);
      if (match && !seen.has(match[1])) {
        seen.add(match[1]);
        counts.set(match[1], (counts.get(match[1]) || 0) + 1);
      }
    }
  }
  const [entity, count] = [...counts.entries()].sort((left, right) => right[1] - left[1])[0] || [];
  return entity && count > flaggedBullets.length / 2
    ? `Most rejected edits tried to add “${entity}” to a bullet that doesn't currently describe it. Nothing was changed there because that would be an unsupported claim, not because of a technical error.`
    : "";
}

function copyBlock(labelText, value, className) {
  const block = document.createElement("div");
  block.className = `copy ${className}`;
  const label = document.createElement("span");
  label.className = "copy-label";
  label.textContent = labelText;
  block.append(label, document.createTextNode(value));
  return block;
}

function renderResults() {
  hideViews();
  $("#company").textContent = state.analysis.company;
  $("#role").textContent = state.analysis.role;
  renderFitAndKeywords();

  const flaggedCount = state.edits.filter((edit) => !edit.traceable).length;
  const selectableCount = state.edits.length - flaggedCount;
  $("#edit-summary").textContent = state.edits.length === 0
    ? "No edits were proposed for this posting — the resume will compile unchanged unless you go back and try a more closely matching posting."
    : `${state.edits.length} edit${state.edits.length === 1 ? "" : "s"} proposed`
      + (flaggedCount ? `, ${flaggedCount} safety-flagged (see reasons below)` : "")
      + (selectableCount ? `, ${selectableCount} selected` : ", none selectable");
  const rejectionSummary = commonRejectedEntitySummary();
  $("#rejection-summary").textContent = rejectionSummary;
  $("#rejection-summary").classList.toggle("hidden", !rejectionSummary);

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
    card.append(
      head,
      reason,
      copyBlock("Current: ", edit.original_text || "Target not found", "old"),
      copyBlock("Proposed: ", edit.new_text, "new")
    );
    if (!edit.traceable) {
      const flag = document.createElement("div"); flag.className = "flag"; flag.textContent = edit.issues.join(" · "); card.append(flag);
    }
    return card;
  }));
  show("#results");
}

function applyTailorState(tailorState) {
  if (!tailorState || tailorState.status === "idle") return;
  clearError();
  if (tailorState.status === "running") {
    const fitProgress = tailorState.fit ? ` Fit: ${tailorState.fit.score}%.` : "";
    setProgress(`${tailorState.step || "Working…"}${fitProgress}`);
  } else if (tailorState.status === "done") {
    state.analysis = {
      company: tailorState.company || "Company",
      role: tailorState.role || "Role",
      keywords: tailorState.keywords || [],
      fit: tailorState.fit || null
    };
    state.edits = tailorState.edits || [];
    renderResults();
  } else if (tailorState.status === "error") {
    hideViews(); showError(tailorState.error); show("#intro");
  }
}

function renderLetter() {
  hideViews();
  const hasIssues = state.paragraphs.some((paragraph) => (paragraph.issues || []).length);
  $("#letter-heading").textContent = `${state.analysis.role} at ${state.analysis.company}`;
  $("#letter-paragraphs").replaceChildren(...state.paragraphs.map((paragraph, index) => {
    const card = document.createElement("article");
    card.className = `letter-paragraph${paragraph.issues?.length ? " flagged" : ""}`;
    const label = document.createElement("label");
    label.textContent = `Paragraph ${index + 1}`;
    const textarea = document.createElement("textarea");
    textarea.dataset.index = index;
    textarea.maxLength = 1200;
    textarea.value = paragraph.text;
    label.append(textarea);
    card.append(label);
    if (paragraph.issues?.length) {
      const flag = document.createElement("div");
      flag.className = "flag";
      flag.textContent = paragraph.issues.join(" · ");
      card.append(flag);
    }
    return card;
  }));
  $("#confirm-letter").checked = false;
  $("#letter-confirm-wrap").classList.toggle("hidden", !hasIssues);
  show("#letter");
}

function applyLetterState(letterState) {
  if (!letterState) return;
  clearError();
  if (letterState.status === "running") {
    setProgress(letterState.step || "Drafting a grounded cover letter…");
  } else if (letterState.status === "done") {
    state.paragraphs = letterState.paragraphs || [];
    renderLetter();
  } else if (letterState.status === "error") {
    renderResults();
    showError(letterState.error);
  }
}

ext.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if (changes.tailorResult?.newValue) applyTailorState(changes.tailorResult.newValue);
  if (changes.letterResult?.newValue) applyLetterState(changes.letterResult.newValue);
});

async function tailor() {
  clearError();
  try {
    setProgress("Reading visible job-posting text…");
    const tabId = await activeTabId();
    const config = await settings();
    const result = await ext.runtime.sendMessage({
      type: "START_TAILOR",
      tabId,
      backendUrl: config.backendUrl,
      sharedSecret: config.sharedSecret
    });
    if (!result?.ok) throw new Error(result?.error || "Could not start the tailoring job.");
  } catch (error) {
    hideViews(); showError(error.message); show("#intro");
  }
}

async function compileResume() {
  clearError();
  const approved = [...document.querySelectorAll(".edit input:checked")].map((input) => {
    const { original_text, traceable, issues, ...proposal } = state.edits[Number(input.dataset.index)];
    return proposal;
  });
  setProgress("Compiling the selected edits…");
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
    await ext.storage.local.remove(["tailorResult", "activeJobId"]);
    $("#done-title").textContent = "Resume PDF ready";
    $("#done-copy").textContent = "Your tailored resume was compiled and sent to browser downloads.";
    hideViews(); show("#done");
  } catch (error) {
    renderResults(); showError(error.message);
  }
}

async function draftLetter() {
  clearError();
  try {
    const stored = await ext.storage.local.get("jobText");
    if (!stored.jobText) throw new Error("The original posting text is unavailable. Tailor the posting again first.");
    const config = await settings();
    setProgress("Starting a grounded cover-letter draft…");
    const result = await ext.runtime.sendMessage({
      type: "START_COVER_LETTER",
      jobText: stored.jobText,
      company: state.analysis.company,
      role: state.analysis.role,
      keywords: state.analysis.keywords,
      backendUrl: config.backendUrl,
      sharedSecret: config.sharedSecret
    });
    if (!result?.ok) throw new Error(result?.error || "Could not start the cover letter.");
  } catch (error) {
    renderResults(); showError(error.message);
  }
}

async function compileLetter() {
  clearError();
  const paragraphs = [...document.querySelectorAll("#letter-paragraphs textarea")].map((textarea) => ({
    text: textarea.value.trim()
  }));
  setProgress("Compiling the cover letter…");
  try {
    const config = await settings();
    const result = await ext.runtime.sendMessage({
      type: "COMPILE_LETTER_AND_DOWNLOAD",
      backendUrl: config.backendUrl,
      sharedSecret: config.sharedSecret,
      payload: {
        company: state.analysis.company,
        role: state.analysis.role,
        keywords: state.analysis.keywords,
        paragraphs,
        confirmed_by_user: $("#confirm-letter").checked
      }
    });
    if (!result?.ok) throw new Error(result?.error || "The cover-letter download failed.");
    await ext.storage.local.remove(["letterResult", "activeLetterJobId"]);
    $("#done-title").textContent = "Cover-letter PDF ready";
    $("#done-copy").textContent = "Your cover letter was compiled and sent to browser downloads.";
    hideViews(); show("#done");
  } catch (error) {
    renderLetter();
    if (/explicit user confirmation|require.*confirmation/i.test(error.message)) {
      show("#letter-confirm-wrap");
    }
    showError(error.message);
  }
}

async function resetTailor() {
  await ext.storage.local.remove([
    "tailorResult", "activeJobId", "jobText", "letterResult", "activeLetterJobId"
  ]);
  state.analysis = null;
  state.edits = [];
  state.paragraphs = [];
  clearError(); hideViews(); show("#intro");
}

$("#tailor").addEventListener("click", tailor);
$("#compile").addEventListener("click", compileResume);
$("#draft-letter").addEventListener("click", draftLetter);
$("#compile-letter").addEventListener("click", compileLetter);
$("#back-to-results").addEventListener("click", renderResults);
$("#new-tailor").addEventListener("click", resetTailor);
$("#restart").addEventListener("click", resetTailor);
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
ext.storage.local.get(["tailorResult", "activeJobId", "letterResult", "activeLetterJobId"]).then((stored) => {
  if (stored.tailorResult) applyTailorState(stored.tailorResult);
  else if (stored.activeJobId) setProgress("Waiting for the backend…");
  if (stored.letterResult) applyLetterState(stored.letterResult);
  else if (stored.activeLetterJobId) setProgress("Waiting for the cover-letter draft…");
}).catch(() => {});
