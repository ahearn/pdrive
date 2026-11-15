// pdrive bridge: a thin JSON-lines wrapper around the official Proton Drive SDK (public-link client).
//
// The Python CLI owns all logic (paths, recursion, skip rules); this process only performs
// single, uid-based SDK operations. One request per stdin line, one response per stdout line:
//
//   -> {"id": 1, "op": "auth", "url": "...", "password": "..."}
//   <- {"id": 1, "ok": true, "result": {...}}
//   <- {"id": 1, "ok": false, "error": {"code": "not_found", "message": "..."}}
//   <- {"id": 1, "event": "progress", "bytes": 123}          (upload/download only)
//
// Requests may overlap (parallel transfers); responses carry the request id.
// Run: node --conditions=browser --import tsx bridge.ts
import "@protontech/crypto/polyfill"; // MUST be first: Node 24 lacks Uint8Array.toBase64/fromBase64
import { CryptoProxy } from "@protontech/crypto";
import { Api as CryptoApi } from "@protontech/crypto/proxy/endpoint/api.ts";
import { getSrp, computeKeyPassword, generateKeySalt } from "@protontech/crypto/srp";
import {
  ProtonDriveClient, MemoryCache, NullFeatureFlagProvider, OpenPGPCryptoWithCryptoProxy, NodeType,
} from "@protontech/drive-sdk";
import type { ProtonDriveAccount, ProtonDriveTelemetry } from "@protontech/drive-sdk";
import fs from "node:fs";
import fsp from "node:fs/promises";
import readline from "node:readline";
import { Readable, Writable } from "node:stream";

CryptoApi.init({});
CryptoProxy.setEndpoint(new CryptoApi());

// ---------------------------------------------------------------------------
// SDK plumbing
// ---------------------------------------------------------------------------
const anonymousAccount: ProtonDriveAccount = {
  getOwnPrimaryAddress: () => Promise.reject(new Error("anonymous session")),
  getOwnAddresses: () => Promise.reject(new Error("anonymous session")),
  getOwnAddress: () => Promise.reject(new Error("anonymous session")),
  hasProtonAccount: async () => false,
  getPublicKeys: async () => [],
} as any;

const sdkLog = (level: string) => (msg: string) => {
  if (process.env.PDRIVE_BRIDGE_DEBUG) process.stderr.write(`sdk ${level}: ${msg}\n`);
};
const telemetry: ProtonDriveTelemetry = {
  getLogger: () => ({ debug: sdkLog("debug"), info: sdkLog("info"), warn: sdkLog("warn"), error: sdkLog("error") }),
  recordMetric: () => {},
} as any;

const fetchWith = (r: any, body: any) => {
  const h = new Headers(r.headers);
  h.set("x-pm-appversion", "web-drive@5.2.0");
  const signal = AbortSignal.any([AbortSignal.timeout(r.timeoutMs), ...(r.signal ? [r.signal] : [])]);
  return fetch(r.url, { method: r.method, headers: h, body, signal });
};
const httpClient = {
  fetchJson: (r: any) => fetchWith(r, r.json !== undefined ? JSON.stringify(r.json) : r.body),
  fetchBlob: (r: any) => fetchWith(r, r.body),
};
const srpModule = {
  getSrp: (version: number, modulus: string, serverEphemeral: string, salt: string, password: string) =>
    getSrp({ Version: version, Modulus: modulus, ServerEphemeral: serverEphemeral, Salt: salt }, { password }),
  getSrpVerifier: () => Promise.reject(new Error("not supported")),
  computeKeyPassword,
  generateKeySalt,
};

function newClient() {
  return new ProtonDriveClient({
    httpClient, entitiesCache: new MemoryCache(), cryptoCache: new MemoryCache(), account: anonymousAccount,
    openPGPCryptoModule: new OpenPGPCryptoWithCryptoProxy(CryptoProxy as any), srpModule: srpModule as any,
    telemetry, featureFlagProvider: new NullFeatureFlagProvider(),
  });
}

// ---------------------------------------------------------------------------
// Session state
// ---------------------------------------------------------------------------
let pub: any = null;
let creds: { url: string; password: string } | null = null;
let rateBps = 0;
let rateNextAt = 0;

async function authenticate(): Promise<any> {
  if (!creds) throw Object.assign(new Error("not authenticated"), { code: "auth" });
  try {
    pub = await newClient().experimental.authURLAccess(creds.url, creds.password, true);
  } catch (e) {
    throw Object.assign(new Error(`authentication failed: ${msg(e)}`), { code: "auth" });
  }
  return pub;
}

// Proton public-link sessions occasionally go bad mid-run ("You don't have permission...").
// Re-authenticate once and retry before reporting the error.
async function withReauth<T>(fn: () => Promise<T>): Promise<T> {
  try {
    return await fn();
  } catch (e) {
    if (classify(e) !== "permission") throw e;
    await authenticate();
    return await fn();
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function msg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function classify(e: any): string {
  if (e?.code && typeof e.code === "string") return e.code;
  const m = msg(e);
  const name = e?.constructor?.name ?? "";
  if (/NodeWithSameNameExists/i.test(name) || /already exists/i.test(m)) return "exists";
  if (/does not belong to the current user session/i.test(m)) return "not_owner";
  if (/not found|does not exist/i.test(m)) return "not_found";
  if (/permission|not allowed|forbidden/i.test(m)) return "permission";
  if (/RateLimited/i.test(name) || /too many requests/i.test(m)) return "rate_limited";
  if (/Connection|Abort/i.test(name) || /fetch failed|timeout|ECONN|ENOTFOUND|EAI_AGAIN/i.test(m)) return "network";
  return "error";
}

function unwrap<T>(v: any): T | undefined {
  // SDK fields are sometimes Result<T,E> ({ok, value}) and sometimes plain values.
  if (v && typeof v === "object" && "ok" in v) return v.ok ? v.value : undefined;
  return v;
}

function nodeJson(n: any) {
  const rev: any = unwrap(n.activeRevision);
  const mtime: Date | undefined = rev?.claimedModificationTime ?? n.modificationTime ?? n.creationTime;
  return {
    uid: n.uid,
    name: unwrap<string>(n.name) ?? null,
    type: n.type === NodeType.Folder ? "folder" : "file",
    size: n.type === NodeType.Folder ? null : (rev?.claimedSize ?? n.totalStorageSize ?? null),
    mtime: mtime ? new Date(mtime).toISOString() : null,
    media_type: n.mediaType ?? null,
  };
}

async function* throttled(src: AsyncIterable<Buffer>): AsyncGenerator<Buffer> {
  for await (const chunk of src) {
    if (rateBps > 0) {
      const now = Date.now();
      const start = Math.max(rateNextAt, now);
      rateNextAt = start + (chunk.length / rateBps) * 1000;
      if (start > now) await new Promise((r) => setTimeout(r, start - now));
    }
    yield chunk;
  }
}

// ---------------------------------------------------------------------------
// Operations (all uid-based)
// ---------------------------------------------------------------------------
type Emit = (event: object) => void;
type Op = (req: any, emit: Emit) => Promise<unknown>;

const ops: Record<string, Op> = {
  async auth(req) {
    creds = { url: req.url, password: req.password ?? "" };
    const p = await authenticate();
    const root = await p.getRootNode();
    return { root: nodeJson(root), role: root.directRole ?? null };
  },

  async configure(req) {
    if (typeof req.rate_bps === "number") rateBps = Math.max(0, req.rate_bps);
    return {};
  },

  async children(req) {
    return withReauth(async () => {
      const out: object[] = [];
      for await (const n of pub.iterateFolderChildren(req.uid)) out.push(nodeJson(n));
      return out;
    });
  },

  async mkdir(req) {
    return withReauth(async () => nodeJson(await pub.createFolder(req.parent, req.name)));
  },

  async rename(req) {
    return withReauth(async () => nodeJson(await pub.renameNode(req.uid, req.name)));
  },

  async delete(req) {
    return withReauth(async () => {
      const out: object[] = [];
      for await (const r of pub.deleteNodes(req.uids)) {
        out.push(r.ok ? { uid: r.uid, ok: true } : { uid: r.uid, ok: false, error: { code: classify(r.error), message: msg(r.error) } });
      }
      return out;
    });
  },

  async download(req, emit) {
    return withReauth(async () => {
      const downloader = await pub.getFileDownloader(req.uid);
      const fh = fs.createWriteStream(req.path);
      const controller = downloader.downloadToStream(Writable.toWeb(fh) as WritableStream, (bytes: number) =>
        emit({ event: "progress", bytes }));
      await controller.completion();
      const { size } = await fsp.stat(req.path);
      return { bytes: size };
    });
  },

  async upload(req, emit) {
    return withReauth(async () => {
      const metadata = {
        mediaType: req.media_type ?? "application/octet-stream",
        expectedSize: req.size,
        modificationTime: req.mtime ? new Date(req.mtime) : undefined,
        overrideExistingDraftByOtherClient: true,
      };
      const uploader = req.replace_uid
        ? await pub.getFileRevisionUploader(req.replace_uid, metadata)
        : await pub.getFileUploader(req.parent, req.name, metadata);
      const src = fs.createReadStream(req.path, { highWaterMark: 256 * 1024 });
      const stream = Readable.toWeb(Readable.from(throttled(src))) as ReadableStream;
      const controller = await uploader.uploadFromStream(stream, [], (bytes: number) =>
        emit({ event: "progress", bytes }));
      const result = await controller.completion();
      try {
        return nodeJson(await pub.getNode(result.nodeUid));
      } catch {
        // The upload is done; don't report it as failed (a retry would then hit "already exists").
        return { uid: result.nodeUid, name: req.name, type: "file", size: req.size, mtime: req.mtime ?? null,
                 media_type: metadata.mediaType };
      }
    });
  },
};

// ---------------------------------------------------------------------------
// Main loop
// ---------------------------------------------------------------------------
function send(obj: object) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

async function handle(line: string) {
  let req: any;
  try {
    req = JSON.parse(line);
  } catch {
    send({ id: null, ok: false, error: { code: "protocol", message: "invalid JSON" } });
    return;
  }
  const op = ops[req.op];
  if (!op) {
    send({ id: req.id, ok: false, error: { code: "protocol", message: `unknown op: ${req.op}` } });
    return;
  }
  if (req.op !== "auth" && req.op !== "configure" && !pub) {
    send({ id: req.id, ok: false, error: { code: "auth", message: "not authenticated" } });
    return;
  }
  try {
    const result = await op(req, (event) => send({ id: req.id, ...event }));
    send({ id: req.id, ok: true, result });
  } catch (e) {
    send({ id: req.id, ok: false, error: { code: classify(e), message: msg(e) } });
  }
}

const pending = new Set<Promise<void>>();
const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on("line", (line) => {
  if (!line.trim()) return;
  const p = handle(line).finally(() => pending.delete(p));
  pending.add(p);
});
rl.on("close", async () => {
  await Promise.allSettled(pending);
  process.exit(0);
});
