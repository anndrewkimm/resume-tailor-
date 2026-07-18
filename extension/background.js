const ext = globalThis.browser ?? globalThis.chrome;

function localBackendUrl(value) {
  const url = new URL((value || "").replace(/\/$/, ""));
  if (url.protocol !== "http:" || !["127.0.0.1", "localhost"].includes(url.hostname)) {
    throw new Error("Backend URL must be a local http://127.0.0.1 or http://localhost address.");
  }
  return url.href.replace(/\/$/, "");
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

async function startTailor({ tabId, backendUrl, sharedSecret }) {
  const url = localBackendUrl(backendUrl);
  await ext.storage.local.remove(["tailorResult", "activeJobId"]);
  const jobText = await extractJobPosting(tabId);
  const { job_id: jobId } = await callApi(url, sharedSecret, "/tailor/start", { job_text: jobText });
  if (!jobId) throw new Error("Backend did not return a tailoring job ID.");

  await ext.storage.local.set({
    activeJobId: jobId,
    jobText,
    backendUrl: url,
    sharedSecret: sharedSecret || "",
    tailorResult: { status: "running", step: "Extracting role requirements…" }
  });
  await ext.alarms.create("tailor-poll", { periodInMinutes: 1 / 15 });
}

async function startCoverLetter({ jobText, company, role, keywords, backendUrl, sharedSecret }) {
  const url = localBackendUrl(backendUrl);
  await ext.storage.local.remove(["letterResult", "activeLetterJobId"]);
  const { job_id: jobId } = await callApi(url, sharedSecret, "/cover-letter/start", {
    job_text: jobText,
    company,
    role,
    keywords
  });
  if (!jobId) throw new Error("Backend did not return a cover-letter job ID.");
  await ext.storage.local.set({
    activeLetterJobId: jobId,
    backendUrl: url,
    sharedSecret: sharedSecret || "",
    letterResult: { status: "running", step: "Drafting a grounded cover letter…" }
  });
  await ext.alarms.create("letter-poll", { periodInMinutes: 1 / 15 });
}

async function pollStoredJob({ alarmName, jobKey, resultKey, statusPath }) {
  const stored = await ext.storage.local.get([jobKey, "backendUrl", "sharedSecret"]);
  const jobId = stored[jobKey];
  if (!jobId) {
    await ext.alarms.clear(alarmName);
    return;
  }

  const backendUrl = localBackendUrl(stored.backendUrl);
  try {
    const response = await fetch(
      `${backendUrl}${statusPath}/${encodeURIComponent(jobId)}`,
      { headers: { "X-Extension-Secret": stored.sharedSecret || "" } }
    );
    if (!response.ok) {
      let detail = `Backend returned ${response.status}`;
      try {
        const payload = await response.json();
        detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail, null, 2);
      } catch {}
      await ext.storage.local.set({
        [resultKey]: { status: "error", step: "", error: detail }
      });
      await ext.alarms.clear(alarmName);
      await ext.storage.local.remove(jobKey);
      return;
    }

    const result = await response.json();
    await ext.storage.local.set({ [resultKey]: result });
    if (result.status !== "running") {
      await ext.alarms.clear(alarmName);
      await ext.storage.local.remove(jobKey);
    }
  } catch (error) {
    // Keep the durable alarm and job ID so a temporary backend/network outage
    // can recover on the next poll instead of losing a completed backend job.
    await ext.storage.local.set({
      [resultKey]: { status: "running", step: `Waiting for backend… ${error.message}` }
    });
  }
}

ext.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === "tailor-poll") {
    await pollStoredJob({
      alarmName: "tailor-poll",
      jobKey: "activeJobId",
      resultKey: "tailorResult",
      statusPath: "/tailor/status"
    });
  } else if (alarm.name === "letter-poll") {
    await pollStoredJob({
      alarmName: "letter-poll",
      jobKey: "activeLetterJobId",
      resultKey: "letterResult",
      statusPath: "/cover-letter/status"
    });
  }
});

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

async function compileAndDownload(message, endpoint = "/compile", fallbackName = "Tailored_Resume.pdf") {
  const backendUrl = localBackendUrl(message.backendUrl);
  const response = await fetch(`${backendUrl}${endpoint}`, {
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
  const filename = disposition.match(/filename="([^"]+)"/)?.[1] || fallbackName;
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
    compileAndDownload(message, "/compile", "Tailored_Resume.pdf")
      .then(sendResponse)
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }
  if (message?.type === "COMPILE_LETTER_AND_DOWNLOAD") {
    compileAndDownload(message, "/cover-letter/compile", "Cover_Letter.pdf")
      .then(sendResponse)
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }
  if (message?.type === "START_TAILOR") {
    startTailor(message)
      .then(() => sendResponse({ ok: true }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }
  if (message?.type === "START_COVER_LETTER") {
    startCoverLetter(message)
      .then(() => sendResponse({ ok: true }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }
  return false;
});
