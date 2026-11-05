"""Contract test for the real Cloudflare Worker (cloudflare/gateway-worker.js).

The Python tests fake the gateway's HTTP interface; this one executes
the actual Worker module under Node with a fake R2 binding, so the
download leg a stateless runner depends on is proven against the code
that will serve it:

* bearer auth fails closed (401 before any R2 access, no write on 401)
* PUT -> GET is byte-identical (the Worker must not re-encode the gzip
  body, which would break the stored DB)
* HEAD returns X-Object-Size and an ETag equal to the payload MD5
  (storage._gw_verify compares exactly that)
* 404 for a missing key, 204 on DELETE, key decoding for slashes/space

Run: python3 tests/test_worker_contract.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKER = (HERE.parent / "cloudflare" / "gateway-worker.js").resolve()

HARNESS = r"""
import crypto from "node:crypto";

const workerUrl = process.argv[2];
const worker = (await import("file://" + workerUrl)).default;

let passed = 0;
const fails = [];
function check(name, cond) {
  if (cond) { passed++; console.log("PASS " + name); }
  else { fails.push(name); console.log("FAIL " + name); }
}

const store = new Map();
const env = {
  GATEWAY_TOKEN: "tok",
  DB: {
    async put(key, body) {
      const buf = Buffer.from(await new Response(body).arrayBuffer());
      store.set(key, buf);
    },
    async get(key) {
      const b = store.get(key);
      return b ? { body: b } : null;
    },
    async head(key) {
      const b = store.get(key);
      if (!b) return null;
      return { size: b.length, httpEtag: '"' + crypto.createHash("md5").update(b).digest("hex") + '"' };
    },
    async delete(key) { store.delete(key); },
  },
};

const base = "https://gw.test/";
function call(method, path, { body, auth = "Bearer tok" } = {}) {
  const headers = {};
  if (auth !== null) headers.Authorization = auth;
  return worker.fetch(new Request(base + path, { method, body, headers }), env);
}
const bytes = async (res) => new Uint8Array(await res.arrayBuffer());

// --- auth -------------------------------------------------------------
check("no Authorization -> 401", (await call("GET", "estama.db.gz", { auth: null })).status === 401);
check("wrong token -> 401", (await call("GET", "estama.db.gz", { auth: "Bearer nope" })).status === 401);
check("401 on PUT writes nothing", store.size === 0 && (await call("PUT", "x.gz", { body: new Uint8Array([1]), auth: "Bearer nope" })).status === 401);
check("still nothing stored", store.size === 0);

// --- PUT -> GET round trip -------------------------------------------
// Stand-in for a gzipped SQLite file: not valid gzip on purpose, the
// Worker must move bytes and never inspect or re-encode them.
const payload = new Uint8Array(1024 * 200);
for (let i = 0; i < payload.length; i++) payload[i] = (i * 31 + 7) & 0xff;

let res = await call("PUT", "estama.db.gz", { body: payload });
check("PUT -> 200", res.status === 200);
check("stored under the literal key", store.has("estama.db.gz"));
check("stored length matches", store.get("estama.db.gz").length === payload.length);

res = await call("GET", "estama.db.gz");
const got = await bytes(res);
check("GET -> 200", res.status === 200);
check("GET is byte-identical", Buffer.compare(Buffer.from(got), Buffer.from(payload)) === 0);
check("GET does not append or trim", got.length === payload.length);

// --- HEAD -------------------------------------------------------------
res = await call("HEAD", "estama.db.gz");
const md5 = crypto.createHash("md5").update(Buffer.from(payload)).digest("hex");
check("HEAD -> 200", res.status === 200);
check("HEAD X-Object-Size is the stored size", res.headers.get("X-Object-Size") === String(payload.length));
check("HEAD ETag is the payload MD5", (res.headers.get("ETag") || "").replace(/"/g, "") === md5);

// --- missing / delete --------------------------------------------------
res = await call("GET", "absent.db.gz");
check("missing GET -> 404", res.status === 404);
res = await call("HEAD", "absent.db.gz");
check("missing HEAD -> 404", res.status === 404);
res = await call("DELETE", "absent.db.gz");
check("DELETE missing -> 204 (idempotent)", res.status === 204);
res = await call("DELETE", "estama.db.gz");
check("DELETE existing -> 204", res.status === 204);
check("deleted object is gone", (await call("GET", "estama.db.gz")).status === 404);

// --- key decoding ------------------------------------------------------
const nested = new Uint8Array([9, 8, 7]);
await call("PUT", "archive/estama-2026-11-04.db.gz", { body: nested });
check("nested key kept as-is", store.has("archive/estama-2026-11-04.db.gz"));
await call("PUT", "a%20b.json", { body: nested });
check("percent-encoded key is decoded", store.has("a b.json"));
check("decoded key reads back", (await call("GET", "a%20b.json")).status === 200);

// --- unsupported method ------------------------------------------------
check("POST on a key -> 405", (await call("POST", "x.json", { body: new Uint8Array([1]) })).status === 405);

// --- no body echoed on errors -----------------------------------------
res = await call("GET", "absent.db.gz");
check("404 body is not the DB", (await res.text()) === "not found");

console.log("RESULT " + (fails.length ? "FAIL " + JSON.stringify(fails) : "ALL PASS"));
process.exit(fails.length ? 1 : 0);
"""

SCRATCH = Path(tempfile.mkdtemp(prefix="estama-worker-test-"))
harness = SCRATCH / "harness.mjs"
harness.write_text(HARNESS, encoding="utf-8")

try:
    proc = subprocess.run(
        ["node", str(harness), str(WORKER)],
        capture_output=True, text=True, timeout=120,
    )
except FileNotFoundError:
    print("SKIP: node is not installed")
    shutil.rmtree(SCRATCH, ignore_errors=True)
    sys.exit(0)

sys.stdout.write(proc.stdout)
sys.stderr.write(proc.stderr)
print("\nRESULT:", "ALL PASS" if proc.returncode == 0 else "FAIL")
shutil.rmtree(SCRATCH, ignore_errors=True)
sys.exit(proc.returncode)
