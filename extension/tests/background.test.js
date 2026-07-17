const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const expectedPdf = Buffer.from("%PDF-1.7\nbackground-download-regression\n%%EOF\n", "ascii");
let messageListener;
let downloadListener;
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

const browser = {
  runtime: {
    onMessage: {
      addListener(callback) { messageListener = callback; }
    }
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
  btoa: (value) => Buffer.from(value, "binary").toString("base64"),
  fetch: async (url, options) => {
    assert.equal(url, "http://127.0.0.1:8765/compile");
    assert.equal(options.headers["X-Extension-Secret"], "test-secret");
    return {
      ok: true,
      status: 200,
      headers: { get: () => 'attachment; filename="Resume_Test_Role.pdf"' },
      async arrayBuffer() {
        return expectedPdf.buffer.slice(
          expectedPdf.byteOffset,
          expectedPdf.byteOffset + expectedPdf.byteLength
        );
      }
    };
  }
});

const source = fs.readFileSync(path.join(__dirname, "..", "background.js"), "utf8");
vm.runInContext(source, context, { filename: "background.js" });

async function main() {
  assert.equal(typeof messageListener, "function");
  assert.equal(typeof downloadListener, "function");
  const response = new Promise((resolve) => {
    const keepAlive = messageListener({
      type: "COMPILE_AND_DOWNLOAD",
      backendUrl: "http://127.0.0.1:8765",
      sharedSecret: "test-secret",
      payload: { approved_edits: [] }
    }, {}, resolve);
    assert.equal(keepAlive, true);
  });

  const result = await response;
  assert.deepEqual(
    JSON.parse(JSON.stringify(result)),
    { ok: true, downloadId: 17, filename: "Resume_Test_Role.pdf" }
  );
  assert.equal(download.filename, "Resume_Test_Role.pdf");
  assert.equal(download.saveAs, true);
  assert.equal(download.url, "blob:resume-tailor/background-pdf");
  assert.doesNotMatch(download.url, /^data:/);
  assert.deepEqual(Buffer.from(await createdBlob.arrayBuffer()), expectedPdf);

  downloadListener({ id: 17, state: { current: "in_progress" } });
  assert.deepEqual(revoked, []);
  downloadListener({ id: 17, state: { current: "complete" } });
  assert.deepEqual(revoked, ["blob:resume-tailor/background-pdf"]);
  downloadListener({ id: 17, state: { current: "interrupted" } });
  assert.equal(revoked.length, 1);
  process.stdout.write("Firefox background Blob download preserves bytes and revokes only at completion\n");
}

main().catch((error) => {
  process.stderr.write(`${error.stack}\n`);
  process.exitCode = 1;
});
