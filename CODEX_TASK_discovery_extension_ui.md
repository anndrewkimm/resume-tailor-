# Task: browsable discovered-jobs list + badge count in the extension popup

## Why this task exists (read this first)

`CODEX_TASK_job_discovery.md` shipped a CLI-only pipeline (`backend/app/discovery.py`,
`python -m backend.app.discovery poll/score/list/dismiss/tailored/report`)
that polls Greenhouse boards for a curated `companies.json` and writes
newly-seen postings to `data/discovered_jobs.jsonl`. PLAN.md §15 explicitly
deferred an in-popup browsable list and a badge count as "fast-follows, not
this task." This task is that fast-follow. See PLAN.md §16.2 for where this
sits in the current roadmap.

Nothing about *discovery* (the Greenhouse polling itself) changes. The
extension still never triggers a Greenhouse fetch — `poll`/`score` stay
CLI-only/manual, consistent with this repo's "no always-on daemon"
precedent (§15). This task only adds a way to **read and act on** postings
someone already discovered by running `poll`/`score` from a terminal: view
them in the popup, open one in a tab, and mark it dismissed/tailored
without switching to a terminal.

## Scope

Backend: two new read/write endpoints in `main.py` that call the existing
`discovery.read_postings()` / `discovery.record_status()` — no changes to
`discovery.py` itself, no new Greenhouse calls, no change to `security.py`
(both endpoints reuse `require_extension_origin`, the same guard every
other endpoint already uses).

Extension: a new popup view listing postings, plus a toolbar badge showing
the count of postings with no status yet ("new"). No new `manifest.json`
permissions are needed — `action`, `storage`, and `alarms` are already
declared (`extension/manifest.json:6`), and the badge and popup calls stay
on the already-allowlisted `host_permissions` backend origin.

## Backend changes

### 1. `backend/app/models.py`

Add three models (near `CompaniesConfig`, since they describe the same
discovery domain):

```python
class DiscoveredPosting(BaseModel):
    id: str
    company: str
    role: str
    location: str
    url: str
    platform: str
    status: str = ""
    fit_score: int | None = None


class DiscoveryListResponse(BaseModel):
    postings: list[DiscoveredPosting] = Field(default_factory=list)


class DiscoveryStatusRequest(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    status: Literal["dismissed", "tailored"]
```

The `status` values must keep matching `discovery.DISCOVERY_STATUSES`
(`discovery.py:20`) — if that tuple ever changes, update this `Literal` in
the same commit; it is intentionally not derived programmatically to avoid
a runtime import of `discovery` inside `models.py`.

### 2. `backend/app/main.py`

Import `discovery` alongside the existing `from . import config, tracker`
(`main.py:11`) and import the three new models into the existing
`from .models import (...)` block (`main.py:17-31`).

Add two endpoints near the other simple reads (e.g. after `/tailor/status`,
before `/cover-letter/start`):

```python
@app.get("/discovery/postings", response_model=DiscoveryListResponse)
def discovery_postings(_: None = Depends(require_extension_origin)) -> DiscoveryListResponse:
    postings = sorted(discovery.read_postings(), key=lambda item: str(item.get("at", "")), reverse=True)
    return DiscoveryListResponse(
        postings=[
            DiscoveredPosting(
                id=str(item["id"]),
                company=str(item.get("company", "")),
                role=str(item.get("role", "")),
                location=str(item.get("location", "")),
                url=str(item.get("url", "")),
                platform=str(item.get("platform", "")),
                status=str(item.get("status") or ""),
                fit_score=item.get("fit_score"),
            )
            for item in postings
        ]
    )


@app.post("/discovery/status")
def discovery_set_status(
    req: DiscoveryStatusRequest, _: None = Depends(require_extension_origin)
) -> dict:
    if not any(str(posting["id"]) == req.id for posting in discovery.read_postings()):
        raise HTTPException(status_code=404, detail="unknown discovery posting id")
    discovery.record_status(req.id, req.status)
    return {"ok": True}
```

Both reuse `discovery.read_postings()`/`discovery.record_status()`
unchanged (`discovery.py:94-97`, `discovery.py:139-156`) — do not
duplicate the JSONL-folding logic in `main.py`. The 404 check exists
because `record_status` itself does not validate the id (it just appends a
`status` event; `read_postings()` silently ignores status events for
unknown ids), and a browser-facing endpoint should reject a bad id
explicitly rather than silently no-op.

No change to the CORS middleware (`main.py:36-43`) — `GET` and `POST` are
already in `allow_methods`.

## Extension changes

### 3. `extension/background.js`

Add a small `fetchApi` GET helper next to the existing `callApi` POST
helper (`background.js:18-33`), mirroring its error-handling shape:

```js
async function fetchApi(backendUrl, sharedSecret, path) {
  const response = await fetch(`${backendUrl}${path}`, {
    headers: { "X-Extension-Secret": sharedSecret || "" }
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

async function listDiscoveredPostings({ backendUrl, sharedSecret }) {
  const url = localBackendUrl(backendUrl);
  return fetchApi(url, sharedSecret, "/discovery/postings");
}

async function setDiscoveryStatus({ backendUrl, sharedSecret, id, status }) {
  const url = localBackendUrl(backendUrl);
  return callApi(url, sharedSecret, "/discovery/status", { id, status });
}

async function refreshDiscoveryBadge() {
  const stored = await ext.storage.local.get(["backendUrl", "sharedSecret"]);
  if (!stored.backendUrl) return;
  try {
    const { postings } = await listDiscoveredPostings(stored);
    const newCount = postings.filter((posting) => !posting.status).length;
    await ext.action.setBadgeBackgroundColor({ color: "#176c49" });
    await ext.action.setBadgeText({ text: newCount > 0 ? String(newCount) : "" });
  } catch {
    // Backend down/unreachable is routine (it's not always running) — leave
    // whatever badge is already showing rather than clearing it.
  }
}
```

Register message handlers in the existing `ext.runtime.onMessage`
listener (`background.js:208-234`), following the same
`.then(sendResponse).catch(...)` / `return true` shape as `START_TAILOR`:

```js
if (message?.type === "LIST_DISCOVERED_JOBS") {
  listDiscoveredPostings(message)
    .then((result) => sendResponse({ ok: true, ...result }))
    .catch((error) => sendResponse({ ok: false, error: error.message }));
  return true;
}
if (message?.type === "SET_DISCOVERY_STATUS") {
  setDiscoveryStatus(message)
    .then(() => refreshDiscoveryBadge())
    .then(() => sendResponse({ ok: true }))
    .catch((error) => sendResponse({ ok: false, error: error.message }));
  return true;
}
```

Add badge refresh triggers near the existing `ext.alarms.onAlarm` listener
(`background.js:114-130`): an `else if (alarm.name === "discovery-badge-refresh")`
branch calling `refreshDiscoveryBadge()`, plus alarm creation and
startup/install hooks at module scope (mirrors nothing existing exactly,
but keep it this small — no new alarm-management abstraction):

```js
ext.alarms.create("discovery-badge-refresh", { periodInMinutes: 30 });
ext.runtime.onStartup.addListener(refreshDiscoveryBadge);
ext.runtime.onInstalled.addListener(refreshDiscoveryBadge);
```

Thirty minutes is deliberately coarse — this only re-reads already-local
data (no Greenhouse call), so the cost of a stale badge for up to 30
minutes is low, and matching `poll`'s own manual/occasional cadence is
appropriate rather than polling the backend aggressively for something the
user rarely changes.

### 4. `extension/popup.html`

Add a button next to `#tailor` inside `#intro` (`popup.html:26-29`):

```html
<button id="view-discovered" class="secondary">Discovered jobs<span id="discovered-badge" class="badge hidden"></span></button>
```

Add a new `<section id="discovered" class="hidden">` (place it after
`#letter`, before `#done`, matching the existing section ordering):

```html
<section id="discovered" class="hidden">
  <p class="eyebrow">FROM LOCAL GREENHOUSE POLLING</p>
  <h2>Discovered jobs</h2>
  <p class="note">Run <code>python -m backend.app.discovery poll</code> and <code>score</code> from a terminal to add more. Opening a posting only opens the tab — use "Tailor this resume" there as usual.</p>
  <label class="confirm"><input id="discovered-hide-handled" type="checkbox" checked><span>Hide dismissed/tailored</span></label>
  <div id="discovered-list"></div>
  <button id="discovered-refresh" class="secondary">Refresh</button>
  <button id="discovered-back" class="secondary reset-results">Back</button>
</section>
```

### 5. `extension/popup.js`

Add `discovered: []` to `state` (`popup.js:4`) and extend `hideViews`
(`popup.js:8`) to include `"#discovered"`.

```js
function renderDiscoveredList() {
  const hideHandled = $("#discovered-hide-handled").checked;
  const rows = state.discovered.filter((posting) => !hideHandled || !posting.status);
  $("#discovered-list").replaceChildren(...rows.map((posting) => {
    const row = document.createElement("article");
    row.className = "discovered-row";
    const link = document.createElement("a");
    link.href = posting.url; link.target = "_blank"; link.rel = "noopener noreferrer";
    link.textContent = `${posting.company} — ${posting.role}`;
    const meta = document.createElement("p");
    meta.className = "note";
    const fit = posting.fit_score === null || posting.fit_score === undefined ? "unscored" : `fit ${posting.fit_score}%`;
    meta.textContent = `${posting.location || "—"} · ${fit} · ${posting.status || "new"}`;
    const actions = document.createElement("div");
    actions.className = "discovered-actions";
    for (const [label, status] of [["Dismiss", "dismissed"], ["Mark tailored", "tailored"]]) {
      const button = document.createElement("button");
      button.className = "secondary";
      button.textContent = label;
      button.disabled = posting.status === status;
      button.addEventListener("click", async () => {
        const config = await settings();
        const result = await ext.runtime.sendMessage({ type: "SET_DISCOVERY_STATUS", id: posting.id, status, ...config });
        if (!result?.ok) { showError(result?.error || "Could not update this posting."); return; }
        await showDiscovered();
      });
      actions.append(button);
    }
    row.append(link, meta, actions);
    return row;
  }));
}

async function showDiscovered() {
  clearError();
  hideViews();
  show("#discovered");
  $("#discovered-list").textContent = "Loading…";
  try {
    const config = await settings();
    const result = await ext.runtime.sendMessage({ type: "LIST_DISCOVERED_JOBS", ...config });
    if (!result?.ok) throw new Error(result?.error || "Could not load discovered postings.");
    state.discovered = result.postings || [];
    renderDiscoveredList();
  } catch (error) {
    showError(error.message);
  }
}
```

Wire listeners near the other `$(...).addEventListener` calls
(`popup.js:305-321`):

```js
$("#view-discovered").addEventListener("click", showDiscovered);
$("#discovered-refresh").addEventListener("click", showDiscovered);
$("#discovered-hide-handled").addEventListener("change", renderDiscoveredList);
$("#discovered-back").addEventListener("click", () => { hideViews(); show("#intro"); });
```

On popup load, alongside the existing `settings().then(...)` call
(`popup.js:323`), populate the intro badge from storage without a network
call (the background alarm already keeps the toolbar badge fresh; reading
`ext.action.getBadgeText({})` here keeps the popup's inline count
consistent with the toolbar rather than triggering a second fetch):

```js
ext.action.getBadgeText({}).then((text) => {
  if (!text) return;
  $("#discovered-badge").textContent = text;
  $("#discovered-badge").classList.remove("hidden");
});
```

### 6. `extension/popup.css`

Two small additions — reuse existing tokens (`--green`, `--line`, `--card`,
`--muted`), do not introduce new colors:

```css
.badge { margin-left:6px; padding:1px 6px; border-radius:99px; background:var(--green); color:white; font-size:11px; font-weight:800; }
.discovered-row { margin:10px 0; padding:10px 12px; border:1px solid var(--line); border-radius:9px; background:var(--card); }
.discovered-row a { display:block; font-weight:800; color:var(--ink); text-decoration:none; }
.discovered-actions { display:flex; gap:8px; margin-top:8px; }
.discovered-actions button { padding:6px 10px; font-size:12px; }
```

## Verification

- Backend: new API tests in `backend/tests/test_api.py` (mirroring the
  existing mocked-auth style) — `/discovery/postings` returns an empty
  list when `data/discovered_jobs.jsonl` doesn't exist, returns seeded
  postings sorted newest-first when it does; `/discovery/status` 404s on
  an unknown id and 200s + persists a status change on a known one (assert
  by calling `discovery.read_postings()` afterward); both reject requests
  missing `X-Extension-Secret` the same way every other endpoint already
  does (reuse the existing auth-guard test pattern rather than writing a
  new one from scratch).
- `python -m unittest discover -s backend/tests -v` passes.
- `node --check extension/background.js`, `node --check extension/popup.js`
  pass; `node extension/tests/background.test.js` passes unmodified (this
  task doesn't touch the compile/download path it covers).
- Manual: with the backend running, seed a few postings via
  `python -m backend.app.discovery poll` against real or a temporary
  `companies.json`, open the popup, confirm the toolbar badge shows the
  new-postings count, click "Discovered jobs", confirm the list renders,
  clicking a posting opens its real URL in a new tab, "Dismiss" and "Mark
  tailored" persist (re-run `python -m backend.app.discovery list` from a
  terminal and confirm the status stuck), and the badge count drops
  accordingly on the next refresh.
