const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "background.js"), "utf8");
const expectedPdf = Buffer.from("%PDF-1.7\nbackground-download-regression\n%%EOF\n", "ascii");
const sharedStorage = {};
const alarmOperations = [];
let download;
let createdBlob;
const revoked = [];

class FirefoxURL extends URL {
  static createObjectURL(blob) {
    createdBlob = blob;
    return "blob:resume-tailor/background-pdf";
  }

  static revokeObjectURL(url) {
    revoked.push(url);
  }
}

function selectedStorage(keys) {
  if (typeof keys === "string") return { [keys]: sharedStorage[keys] };
  return Object.fromEntries(keys.filter((key) => key in sharedStorage).map((key) => [key, sharedStorage[key]]));
}

function createWorker(fetchImpl) {
  let messageListener;
  let alarmListener;
  let downloadListener;

  const browser = {
    runtime: {
      onMessage: {
        addListener(callback) { messageListener = callback; }
      }
    },
    scripting: {
      async executeScript(options) {
        assert.deepEqual(
          JSON.parse(JSON.stringify(options)),
          { target: { tabId: 41 }, files: ["content.js"] }
        );
      }
    },
    tabs: {
      async sendMessage(tabId, message) {
        assert.equal(tabId, 41);
        assert.deepEqual(JSON.parse(JSON.stringify(message)), { type: "EXTRACT_JOB_POSTING" });
        return { ok: true, text: "A long software engineering job posting with REST APIs and Python. ".repeat(3) };
      }
    },
    storage: {
      local: {
        async get(keys) { return selectedStorage(keys); },
        async set(values) { Object.assign(sharedStorage, values); },
        async remove(keys) {
          for (const key of (Array.isArray(keys) ? keys : [keys])) delete sharedStorage[key];
        }
      }
    },
    alarms: {
      onAlarm: {
        addListener(callback) { alarmListener = callback; }
      },
      async create(name, options) { alarmOperations.push({ action: "create", name, options }); },
      async clear(name) { alarmOperations.push({ action: "clear", name }); return true; }
    },
    downloads: {
      onChanged: {
        addListener(callback) { downloadListener = callback; }
      },
      async search(query) {
        assert.deepEqual(query, { id: 17 });
        return [{ id: 17, state: "in_progress" }];
      },
      async download(options) {
        download = options;
        return 17;
      }
    }
  };

  const context = vm.createContext({
    browser,
    Blob,
    URL: FirefoxURL,
    Uint8Array,
    encodeURIComponent,
    btoa: (value) => Buffer.from(value, "binary").toString("base64"),
    fetch: fetchImpl
  });
  vm.runInContext(source, context, { filename: "background.js" });
  return {
    sendMessage(message) {
      return new Promise((resolve) => {
        const keepAlive = messageListener(message, {}, resolve);
        assert.equal(keepAlive, true);
      });
    },
    alarmListener,
    downloadListener
  };
}

async function testPdfDownload() {
  const worker = createWorker(async (url, options) => {
    assert.equal(url, "http://127.0.0.1:8765/compile");
    assert.equal(options.headers["X-Extension-Secret"], "test-secret");
    return {
      ok: true,
      status: 200,
      headers: { get: () => 'attachment; filename="Resume_Test_Role.pdf"' },
      async arrayBuffer() {
        return expectedPdf.buffer.slice(expectedPdf.byteOffset, expectedPdf.byteOffset + expectedPdf.byteLength);
      }
    };
  });

  const result = await worker.sendMessage({
    type: "COMPILE_AND_DOWNLOAD",
    backendUrl: "http://127.0.0.1:8765",
    sharedSecret: "test-secret",
    payload: { approved_edits: [] }
  });
  assert.deepEqual(
    JSON.parse(JSON.stringify(result)),
    { ok: true, downloadId: 17, filename: "Resume_Test_Role.pdf" }
  );
  assert.equal(download.filename, "Resume_Test_Role.pdf");
  assert.equal(download.saveAs, true);
  assert.equal(download.url, "blob:resume-tailor/background-pdf");
  assert.doesNotMatch(download.url, /^data:/);
  assert.deepEqual(Buffer.from(await createdBlob.arrayBuffer()), expectedPdf);

  worker.downloadListener({ id: 17, state: { current: "in_progress" } });
  assert.deepEqual(revoked, []);
  worker.downloadListener({ id: 17, state: { current: "complete" } });
  assert.deepEqual(revoked, ["blob:resume-tailor/background-pdf"]);
  worker.downloadListener({ id: 17, state: { current: "interrupted" } });
  assert.equal(revoked.length, 1);
}

async function testTailorPollingSurvivesWorkerRestart() {
  const firstWorker = createWorker(async (url, options) => {
    assert.equal(url, "http://127.0.0.1:8765/tailor/start");
    assert.equal(options.method, "POST");
    assert.equal(options.headers["X-Extension-Secret"], "test-secret");
    return { ok: true, status: 200, async json() { return { job_id: "durable-job-123" }; } };
  });

  const started = await firstWorker.sendMessage({
    type: "START_TAILOR",
    tabId: 41,
    backendUrl: "http://127.0.0.1:8765",
    sharedSecret: "test-secret"
  });
  assert.deepEqual(JSON.parse(JSON.stringify(started)), { ok: true });
  assert.equal(sharedStorage.activeJobId, "durable-job-123");
  assert.equal(sharedStorage.tailorResult.status, "running");
  assert.deepEqual(JSON.parse(JSON.stringify(alarmOperations.at(-1))), {
    action: "create",
    name: "tailor-poll",
    options: { periodInMinutes: 1 / 15 }
  });

  // A new VM context has none of the first worker's JavaScript memory. Only
  // extension storage and the durable alarm schedule are shared.
  const restartedWorker = createWorker(async (url, options) => {
    assert.equal(url, "http://127.0.0.1:8765/tailor/status/durable-job-123");
    assert.equal(options.headers["X-Extension-Secret"], "test-secret");
    return {
      ok: true,
      status: 200,
      async json() {
        return {
          status: "done",
          step: "Drafting grounded resume edits…",
          company: "Example Co",
          role: "Engineer",
          keywords: [],
          edits: [],
          error: null
        };
      }
    };
  });
  await restartedWorker.alarmListener({ name: "tailor-poll" });

  assert.equal(sharedStorage.activeJobId, undefined);
  assert.equal(sharedStorage.tailorResult.status, "done");
  assert.equal(sharedStorage.tailorResult.company, "Example Co");
  assert.deepEqual(alarmOperations.at(-1), { action: "clear", name: "tailor-poll" });
}

async function testLetterPollingSurvivesWorkerRestart() {
  const firstWorker = createWorker(async (url, options) => {
    assert.equal(url, "http://127.0.0.1:8765/cover-letter/start");
    assert.equal(options.method, "POST");
    const payload = JSON.parse(options.body);
    assert.equal(payload.company, "Example Co");
    assert.equal(payload.keywords[0].term, "Python");
    return { ok: true, status: 200, async json() { return { job_id: "letter-job-456" }; } };
  });
  const started = await firstWorker.sendMessage({
    type: "START_COVER_LETTER",
    jobText: "A long software engineering posting. ".repeat(3),
    company: "Example Co",
    role: "Engineer",
    keywords: [{ term: "Python", category: "technology", importance: "high", evidence: "required" }],
    backendUrl: "http://127.0.0.1:8765",
    sharedSecret: "test-secret"
  });
  assert.deepEqual(JSON.parse(JSON.stringify(started)), { ok: true });
  assert.equal(sharedStorage.activeLetterJobId, "letter-job-456");
  assert.equal(sharedStorage.letterResult.status, "running");

  const restartedWorker = createWorker(async (url) => {
    assert.equal(url, "http://127.0.0.1:8765/cover-letter/status/letter-job-456");
    return {
      ok: true,
      status: 200,
      async json() {
        return { status: "done", step: "", paragraphs: [{ text: "Draft", issues: [] }], error: null };
      }
    };
  });
  await restartedWorker.alarmListener({ name: "letter-poll" });
  assert.equal(sharedStorage.activeLetterJobId, undefined);
  assert.equal(sharedStorage.letterResult.paragraphs[0].text, "Draft");
  assert.deepEqual(alarmOperations.at(-1), { action: "clear", name: "letter-poll" });
}

async function testLetterUsesSharedPdfDownloadLifecycle() {
  const worker = createWorker(async (url, options) => {
    assert.equal(url, "http://127.0.0.1:8765/cover-letter/compile");
    assert.equal(JSON.parse(options.body).confirmed_by_user, true);
    return {
      ok: true,
      status: 200,
      headers: { get: () => 'attachment; filename="CoverLetter_Test_Role.pdf"' },
      async arrayBuffer() {
        return expectedPdf.buffer.slice(expectedPdf.byteOffset, expectedPdf.byteOffset + expectedPdf.byteLength);
      }
    };
  });
  const result = await worker.sendMessage({
    type: "COMPILE_LETTER_AND_DOWNLOAD",
    backendUrl: "http://127.0.0.1:8765",
    sharedSecret: "test-secret",
    payload: { paragraphs: [{ text: "Draft" }], confirmed_by_user: true }
  });
  assert.equal(result.filename, "CoverLetter_Test_Role.pdf");
  assert.equal(download.url, "blob:resume-tailor/background-pdf");
  assert.deepEqual(Buffer.from(await createdBlob.arrayBuffer()), expectedPdf);
  const before = revoked.length;
  worker.downloadListener({ id: 17, state: { current: "complete" } });
  assert.equal(revoked.length, before + 1);
}

async function main() {
  await testPdfDownload();
  await testTailorPollingSurvivesWorkerRestart();
  await testLetterPollingSurvivesWorkerRestart();
  await testLetterUsesSharedPdfDownloadLifecycle();
  process.stdout.write(
    "Firefox shared PDF downloads and durable tailor/letter polling survive background-worker restart\n"
  );
}

main().catch((error) => {
  process.stderr.write(`${error.stack}\n`);
  process.exitCode = 1;
});
