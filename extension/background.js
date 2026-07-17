const ext = globalThis.browser ?? globalThis.chrome;

function localBackendUrl(value) {
  const url = new URL((value || "").replace(/\/$/, ""));
  if (url.protocol !== "http:" || !["127.0.0.1", "localhost"].includes(url.hostname)) {
    throw new Error("Backend URL must be a local http://127.0.0.1 or http://localhost address.");
  }
  return url.href.replace(/\/$/, "");
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
  if (message?.type !== "COMPILE_AND_DOWNLOAD") return false;
  compileAndDownload(message)
    .then(sendResponse)
    .catch((error) => sendResponse({ ok: false, error: error.message }));
  return true;
});
