const ext = globalThis.browser ?? globalThis.chrome;

function localBackendUrl(value) {
  const url = new URL((value || "").replace(/\/$/, ""));
  if (url.protocol !== "http:" || !["127.0.0.1", "localhost"].includes(url.hostname)) {
    throw new Error("Backend URL must be a local http://127.0.0.1 or http://localhost address.");
  }
  return url.href.replace(/\/$/, "");
}

// Runs the extraction + analysis pipeline here in the persistent background
// worker (not in popup.js) so that switching tabs or closing the popup mid-run
// no longer aborts the job. Popups reopen and call GET_TAILOR_STATE to catch
// up on whatever this produced while they were gone.
let tailorState = { status: "idle" };

function broadcastTailorState() {
  ext.runtime.sendMessage({ type: "TAILOR_STATE", state: tailorState }).catch(() => {});
}

async function extractJobPosting(tabId) {
  await ext.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
  const result = await ext.tabs.sendMessage(tabId, { type: "EXTRACT_JOB_POSTING" });
  if (!result?.ok) throw new Error(result?.error || "Could not read this page.");
  return result.text;
}

async function callApi(backendUrl, sharedSecret, path, body) {
  const response = await fetch(`${backendUrl}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Extension-Secret": sharedSecret || "" },
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

async function runTailor({ tabId, backendUrl, sharedSecret }) {
  tailorState = { status: "running", step: "Reading visible job-posting text…" };
  broadcastTailorState();
  try {
    const url = localBackendUrl(backendUrl);
    const jobText = await extractJobPosting(tabId);
    tailorState = { status: "running", step: "Extracting role requirements…" };
    broadcastTailorState();
    const analysis = await callApi(url, sharedSecret, "/extract-keywords", { job_text: jobText });
    tailorState = { status: "running", step: "Drafting grounded resume edits…" };
    broadcastTailorState();
    const diff = await callApi(url, sharedSecret, "/generate-diff", { job_text: jobText, keywords: analysis.keywords });
    tailorState = { status: "done", jobText, analysis, edits: diff.edits };
  } catch (error) {
    tailorState = { status: "error", error: error.message };
  }
  broadcastTailorState();
}

const activeBlobUrls = new Map();

function releaseBlobUrl(downloadId) {
  const revoke = activeBlobUrls.get(downloadId);
  if (!revoke) return;
  activeBlobUrls.delete(downloadId);
  revoke();
}

function pdfDataUrl(buffer) {
  const bytes = new Uint8Array(buffer);
  const chunks = [];
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    chunks.push(String.fromCharCode(...bytes.subarray(offset, offset + 0x8000)));
  }
  return `data:application/pdf;base64,${btoa(chunks.join(""))}`;
}

function pdfDownloadResource(buffer) {
  if (typeof URL.createObjectURL === "function") {
    const url = URL.createObjectURL(new Blob([buffer], { type: "application/pdf" }));
    return { url, revoke: () => URL.revokeObjectURL(url) };
  }
  // Chrome extension service workers don't expose URL.createObjectURL.
  // Chrome accepts data URLs in downloads.download; Firefox event pages
  // take the Blob branch above because Firefox rejects data download URLs.
  return { url: pdfDataUrl(buffer), revoke: null };
}

ext.downloads.onChanged.addListener((delta) => {
  if (!delta.state || !["complete", "interrupted"].includes(delta.state.current)) return;
  releaseBlobUrl(delta.id);
});

async function compileAndDownload(message) {
  const backendUrl = localBackendUrl(message.backendUrl);
  const response = await fetch(`${backendUrl}/compile`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Extension-Secret": message.sharedSecret || ""
    },
    body: JSON.stringify(message.payload)
  });
  if (!response.ok) {
    let detail = `Backend returned ${response.status}`;
    try {
      const payload = await response.json();
      detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail, null, 2);
    } catch {}
    throw new Error(detail);
  }

  const disposition = response.headers.get("Content-Disposition") || "";
  const filename = disposition.match(/filename="([^"]+)"/)?.[1] || "Tailored_Resume.pdf";
  const resource = pdfDownloadResource(await response.arrayBuffer());
  let downloadId;
  try {
    downloadId = await ext.downloads.download({ url: resource.url, filename, saveAs: true });
  } catch (error) {
    resource.revoke?.();
    throw error;
  }
  if (resource.revoke) {
    activeBlobUrls.set(downloadId, resource.revoke);
    // Very small files may reach a terminal state before the listener map is
    // populated. Query once after registration so that race cannot leak the
    // object URL.
    try {
      const [item] = await ext.downloads.search({ id: downloadId });
      if (["complete", "interrupted"].includes(item?.state)) releaseBlobUrl(downloadId);
    } catch {}
  }
  return { ok: true, downloadId, filename };
}

ext.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "COMPILE_AND_DOWNLOAD") {
    compileAndDownload(message)
      .then(sendResponse)
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }
  if (message?.type === "START_TAILOR") {
    if (tailorState.status !== "running") runTailor(message);
    sendResponse({ ok: true });
    return false;
  }
  if (message?.type === "GET_TAILOR_STATE") {
    sendResponse(tailorState);
    return false;
  }
  if (message?.type === "RESET_TAILOR_STATE") {
    tailorState = { status: "idle" };
    sendResponse({ ok: true });
    return false;
  }
  return false;
});
