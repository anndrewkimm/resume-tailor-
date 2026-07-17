(() => {
  if (globalThis.__resumeTailorLoaded) return;
  globalThis.__resumeTailorLoaded = true;
  const ext = globalThis.browser ?? globalThis.chrome;

  function visibleText(element) {
    const clone = element.cloneNode(true);
    clone.querySelectorAll("script,style,noscript,svg,nav,footer,header,aside,[aria-hidden='true']")
      .forEach((node) => node.remove());
    return (clone.innerText || clone.textContent || "")
      .replace(/\u00a0/g, " ")
      .replace(/[ \t]+\n/g, "\n")
      .replace(/\n{3,}/g, "\n\n")
      .trim();
  }

  function extractPosting() {
    const selectors = [
      "main", "article", "[role='main']", "[data-testid*='job']",
      "[class*='job-description']", "[class*='jobDescription']",
      "[id*='job-description']", "[id*='jobDescription']"
    ];
    const candidates = [...new Set(selectors.flatMap((selector) => [...document.querySelectorAll(selector)]))]
      .map((element) => visibleText(element))
      .filter((text) => text.length >= 400);
    const bodyText = visibleText(document.body);
    let text = candidates.sort((a, b) => b.length - a.length)[0] || bodyText;
    if (text.length < 100) throw new Error("This page does not contain enough visible job-posting text.");
    if (text.length > 100000) text = text.slice(0, 100000);
    return `${document.title}\n${location.href}\n\n${text}`;
  }

  ext.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type !== "EXTRACT_JOB_POSTING") return false;
    try {
      sendResponse({ ok: true, text: extractPosting() });
    } catch (error) {
      sendResponse({ ok: false, error: error.message });
    }
    return false;
  });
})();
