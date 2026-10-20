import {DurableObject, WorkerEntrypoint} from "cloudflare:workers";
import {PREPARE_UPGRADE, UpgradeDeferred, containsHandoffMarker} from "./container_upgrade.js";
import {
  DAY, PROVIDER_BODY_LIMIT, SNAPSHOT_LIMIT, cleanHeaders,
  failure, limitedBytes, providerPath, routeKind, runtimeRecoveryAllowed, setVisitorCookie, validOrigin,
  validateProviderBody, visitorCookie,
} from "./policy.js";

const ACTIVE_LEASE = 30 * 60 * 1000;
const INACTIVITY = 15 * 60 * 1000;

async function internal(stub, path, data) {
  return stub.fetch(new Request(`https://internal${path}`, {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(data),
  }));
}

function admission(env, path, data) {
  return internal(env.ADMISSION.getByName("global"), path, data);
}

async function hash(value) {
  return [...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)))]
    .map(byte => byte.toString(16).padStart(2, "0")).join("");
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    // The portfolio Worker strips APP_PATH before invoking this private binding.
    const kind = routeKind(url.pathname, request.method);
    if (url.origin !== env.PUBLIC_ORIGIN || !validOrigin(request, env.PUBLIC_ORIGIN, kind === "dashboard"))
      return failure("Open this application on tranthiennhan.com to continue.");
    if (kind === "blocked") return failure("This endpoint is unavailable.", 404);
    if (request.body && request.headers.get("upgrade")?.toLowerCase() !== "websocket") {
      try {
        const bytes = await limitedBytes(request, 9 * 1024 * 1024);
        request = new Request(request, {body: bytes});
      } catch { return failure("The upload is too large. Files may be up to 8 MB.", 413); }
    }
    const ip = await hash(request.headers.get("cf-connecting-ip") ?? "missing-ip");
    const guard = await admission(env, "/admit", {
      ip, token: visitorCookie(request), agent: kind === "agent", recovery: runtimeRecoveryAllowed(request),
    });
    if (!guard.ok) return guard;
    const {token, created} = await guard.json();
    const headers = cleanHeaders(request.headers);
    headers.set("x-analyst-role", kind);
    const forwarded = new Request(request, {headers});
    const response = kind === "dashboard"
      ? (url.pathname === "/health" ? Response.json({status: "ready", service: "manufacturing-quality-edge"}) : await env.ASSETS.fetch(forwarded))
      : await env.ANALYST.getByName(`visitor:${token}`).fetch(forwarded);
    if (!created) return response;
    const result = new Response(response.body, response);
    result.headers.append("Set-Cookie", setVisitorCookie(token, env.APP_PATH));
    return result;
  },
};

export class Admission extends DurableObject {
  async fetch(request) {
    const path = new URL(request.url).pathname;
    const data = await request.json();
    const now = Date.now();
    if (path === "/admit") {
      return this.ctx.storage.transaction(async tx => {
        const counts = await tx.get(`ip:${data.ip}`) ?? {minute: now, minuteCount: 0, hour: now, issued: 0};
        if (now - counts.minute > 60000) { counts.minute = now; counts.minuteCount = 0; }
        if (now - counts.hour > 3600000) { counts.hour = now; counts.issued = 0; }
        if (++counts.minuteCount > 90) return failure("Too many requests. Please try again in a minute.", 429, 60);
        let token = data.token;
        const existing = token ? await tx.get(`visitor:${token}`) : null;
        let created = !existing || existing.expires < now;
        if (created) {
          if (++counts.issued > 10) return failure("Too many new visits from this connection. Please try again later.", 429, 3600);
          token = [...crypto.getRandomValues(new Uint8Array(32))].map(b => b.toString(16).padStart(2, "0")).join("");
        }
        const visitor = created ? {expires: now + 7 * DAY, minute: now, count: 0} : existing;
        if (now - visitor.minute > 60000) { visitor.minute = now; visitor.count = 0; }
        if (++visitor.count > 120) return failure("Please wait a moment before sending more requests.", 429, 60);
        // Existing visitors can read receipts and cancel work after their lease
        // expires. New visitors and every prompt-capable transport still need capacity.
        if (data.agent && (created || data.recovery !== true)) {
          const active = (await tx.get("active") ?? []).filter(entry => entry.until > now && entry.token !== token);
          if (active.length >= 10) return failure("The analyst is busy helping other visitors. Please try again shortly.", 503, 60);
          active.push({token, until: now + ACTIVE_LEASE});
          await tx.put("active", active);
        }
        await tx.put(`ip:${data.ip}`, counts);
        await tx.put(`visitor:${token}`, visitor);
        const cleanupAt = now + DAY;
        const scheduled = await tx.getAlarm();
        if (scheduled === null || scheduled > cleanupAt) await tx.setAlarm(cleanupAt);
        return Response.json({token, created});
      });
    }
    return failure("Unavailable.", 404);
  }

  async alarm() {
    const now = Date.now();
    // Prune expired identifiers without storing client addresses or chat content.
    for (const prefix of ["visitor:", "ip:"]) {
      const entries = await this.ctx.storage.list({prefix});
      for (const [key, value] of entries) {
        if ((value.expires ?? value.hour + DAY) < now) await this.ctx.storage.delete(key);
      }
    }
    await this.ctx.storage.setAlarm(now + DAY);
  }
}

export class AnalystContainer extends DurableObject {
  starting;
  role;

  constructor(ctx, env) {
    super(ctx, env);
    ctx.storage.sql.exec("CREATE TABLE IF NOT EXISTS runtime_snapshot (id INTEGER PRIMARY KEY CHECK(id=1), etag TEXT NOT NULL, size INTEGER NOT NULL)");
    ctx.storage.sql.exec("CREATE TABLE IF NOT EXISTS runtime_chunks (position INTEGER PRIMARY KEY, value BLOB NOT NULL)");
    ctx.storage.sql.exec("CREATE TABLE IF NOT EXISTS upgrade_snapshot (id INTEGER PRIMARY KEY CHECK(id=1), etag TEXT NOT NULL, size INTEGER NOT NULL)");
    ctx.storage.sql.exec("CREATE TABLE IF NOT EXISTS upgrade_chunks (position INTEGER PRIMARY KEY, value BLOB NOT NULL)");
  }

  async fetch(request) {
    const url = new URL(request.url);
    if (url.hostname === "internal") {
      if (url.pathname === "/snapshot") return this.snapshot(request);
      if (url.pathname === "/upgrade-snapshot") return this.snapshot(request, true);
      if (url.pathname.startsWith("/provider/")) return this.provider(request);
      return failure("Unavailable.", 404);
    }
    const role = request.headers.get("x-analyst-role");
    if (!["agent", "dashboard"].includes(role)) return failure("Unavailable.", 404);
    this.role ??= await this.ctx.storage.get("role");
    if (this.role && this.role !== role) return failure("Invalid runtime role.");
    this.role = role;
    await this.ctx.storage.put("role", role);
    // Clear the previous runtime lifetime alarm when upgrading existing visitors.
    await this.ctx.storage.deleteAlarm();
    this.starting ??= this.start().finally(() => { this.starting = undefined; });
    try { await this.starting; }
    catch (error) {
      if (!(error instanceof UpgradeDeferred) || !runtimeRecoveryAllowed(request))
        return failure("The analyst is updating its workspace. Please try again shortly.", 503, 5);
    }
    url.protocol = "http:";
    url.host = "container";
    const headers = cleanHeaders(request.headers);
    // Preserve the public origin, which the cloud backend validates independently.
    return this.ctx.container.getTcpPort(8000).fetch(new Request(url, new Request(request, {headers})));
  }

  async start() {
    const container = this.ctx.container;
    let upgraded;
    const gateway = this.ctx.exports.ContainerGateway({props: {containerId: this.ctx.id.toString(), role: this.role}});
    if (!container.running && await this.ctx.storage.get("image-upgrade")) {
      upgraded = await this.completeUpgrade();
    }
    if (container.running) {
      await container.interceptAllOutboundHttp(gateway);
      await container.interceptOutboundHttps("*", gateway);
      const info = await container.inspect();
      if (info?.image && info.image !== container.images.base) upgraded = await this.upgrade(info.image);
      else if (!info?.image) throw new UpgradeDeferred("Container startup image is not confirmed");
    }
    if (!container.running) {
      // Intercepts expire when the previous Container exits. Install both rules
      // for this new instance before Python attempts TLS trust or state restore.
      await container.interceptAllOutboundHttp(gateway);
      await container.interceptOutboundHttps("*", gateway);
      container.start({
        image: container.images.base,
        instance: "standard-1",
        enableInternet: false,
        env: {
          ANALYST_CLOUD_ROLE: this.role,
          ANALYST_STATE_URL: "http://state.internal/snapshot",
          OPENAI_MODEL: this.env.OPENAI_MODEL,
          OPENAI_FAST_MODE: this.env.OPENAI_FAST_MODE ?? "false",
        },
      });
    }
    await container.setInactivityTimeout(INACTIVITY);
    for (let i = 0; i < 150; i++) {
      try {
        const result = await container.getTcpPort(8000).fetch("http://container/health", {signal: AbortSignal.timeout(2000)});
        await result.body?.cancel();
        if (result.ok) {
          if (upgraded) {
            const actual = await container.inspect();
            if (actual?.image !== container.images.base) throw new Error("Updated runtime image is not confirmed");
            console.info(JSON.stringify({event: "analyst_image_upgrade", old_image: upgraded.old_image,
              new_image: actual.image, role: this.role, operation: upgraded.marker}));
          }
          return;
        }
      } catch {}
      await scheduler.wait(400);
    }
    throw new Error("Container readiness timed out.");
  }

  async upgrade(oldImage) {
    const container = this.ctx.container;
    if (this.role === "dashboard") {
      // Dashboard Containers hold no visitor workspace or provider credentials.
      container.signal(15);
      await container.monitor();
      return {old_image: oldImage, marker: crypto.randomUUID()};
    }
    const marker = crypto.randomUUID();
    const saved = this.ctx.storage.sql.exec("SELECT etag FROM upgrade_snapshot WHERE id=1").toArray()[0];
    const check = await container.exec(["python", "-c", PREPARE_UPGRADE, marker, "", "inspect"], {cwd: "/app", user: "10001:10001"});
    const checked = await check.output();
    if (checked.exitCode !== 0 || JSON.parse(new TextDecoder().decode(checked.stdout).trim()).status !== "idle")
      throw new UpgradeDeferred("The previous run must settle before updating");
    await this.ctx.storage.put("image-upgrade", {image: container.images.base, old_image: oldImage, marker});
    try {
      const command = await container.exec(["python", "-c", PREPARE_UPGRADE, marker, saved?.etag ?? "", "prepare"], {cwd: "/app", user: "10001:10001"});
      const output = await command.output();
      const result = JSON.parse(new TextDecoder().decode(output.stdout).trim());
      if (output.exitCode !== 0 || result.status !== "prepared") throw new UpgradeDeferred("The previous run must settle before updating");
      const staged = this.ctx.storage.sql.exec("SELECT etag FROM upgrade_snapshot WHERE id=1").toArray()[0];
      if (!staged || staged.etag !== result.staged_etag) throw new Error("Upgrade checkpoint was not acknowledged");
      await container.monitor();
      await this.ctx.storage.put("image-upgrade-exit", {marker, clean: true});
      return await this.completeUpgrade();
    } catch (error) {
      // An active runtime keeps its cancellation and read paths. No forced
      // destruction or generation replay is used to finish an unsafe handoff.
      if (container.running) {
        const release = await container.exec(["python", "-c", PREPARE_UPGRADE, marker, "", "release"], {cwd: "/app", user: "10001:10001"});
        const released = await release.output();
        if (released.exitCode !== 0) throw new Error("The old workspace admission fence could not be released");
        await this.ctx.storage.delete("image-upgrade");
      }
      throw error;
    }
  }

  async completeUpgrade() {
    const pending = await this.ctx.storage.get("image-upgrade");
    const proof = await this.ctx.storage.get("image-upgrade-ack");
    const exit = await this.ctx.storage.get("image-upgrade-exit");
    const current = this.ctx.storage.sql.exec("SELECT etag FROM runtime_snapshot WHERE id=1").toArray()[0];
    if (!pending || exit?.clean !== true || exit.marker !== pending.marker ||
        proof?.marker !== pending.marker || proof.etag !== current?.etag)
      throw new Error("The previous workspace handoff still requires reconciliation");
    await this.ctx.storage.delete("image-upgrade");
    await this.ctx.storage.delete("image-upgrade-ack");
    await this.ctx.storage.delete("image-upgrade-exit");
    return pending;
  }

  async snapshot(request, upgrade = false) {
    if ((await this.ctx.storage.get("role")) !== "agent") return failure("Unavailable.", 404);
    // Active local work pings this private route to keep the container alive
    // while no browser is connected, without reading or replacing its archive.
    if (request.method === "HEAD") return new Response(null, {status: 204});
    const sql = this.ctx.storage.sql;
    const metaTable = upgrade ? "upgrade_snapshot" : "runtime_snapshot";
    const chunkTable = upgrade ? "upgrade_chunks" : "runtime_chunks";
    if (request.method === "GET") {
      // Read a coherent version synchronously before yielding to concurrent writes.
      const meta = sql.exec(`SELECT etag, size FROM ${metaTable} WHERE id=1`).toArray()[0];
      if (!meta) return new Response(null, {status: 404});
      const chunks = sql.exec(`SELECT value FROM ${chunkTable} ORDER BY position`).toArray();
      return new Response(new Blob(chunks.map(row => row.value)), {
        headers: {ETag: meta.etag, "Content-Type": "application/gzip", "Content-Length": String(meta.size)},
      });
    }
    if (request.method !== "PUT") return failure("Unavailable.", 405);
    if (request.headers.get("content-type") !== "application/gzip") return failure("Expected a compressed runtime snapshot.", 415);
    let bytes;
    try { bytes = await limitedBytes(request, SNAPSHOT_LIMIT); }
    catch { return failure("The saved workspace exceeds its storage limit.", 413); }
    const match = request.headers.get("if-match");
    const fresh = request.headers.get("if-none-match") === "*";
    if ((!match && !fresh) || (match && fresh)) return failure("A snapshot version is required.", 428);
    const etag = `"${crypto.randomUUID()}"`;
    const written = this.ctx.storage.transactionSync(() => {
      const previous = sql.exec(`SELECT etag FROM ${metaTable} WHERE id=1`).toArray()[0];
      if ((fresh && previous) || (match && previous?.etag !== match)) return false;
      sql.exec(`DELETE FROM ${chunkTable}`);
      for (let start = 0, position = 0; start < bytes.byteLength; start += 65536, position++)
        sql.exec(`INSERT INTO ${chunkTable} (position, value) VALUES (?, ?)`, position, bytes.slice(start, start + 65536));
      sql.exec(`INSERT OR REPLACE INTO ${metaTable} (id, etag, size) VALUES (1, ?, ?)`, etag, bytes.byteLength);
      return true;
    });
    if (written && !upgrade) {
      const pending = await this.ctx.storage.get("image-upgrade");
      if (pending && await containsHandoffMarker(bytes, pending.marker))
        await this.ctx.storage.put("image-upgrade-ack", {marker: pending.marker, etag});
    }
    return written ? new Response(null, {status: 204, headers: {ETag: etag}})
      : failure("The saved workspace changed. Reload before trying again.", 412);
  }

  async provider(request) {
    if ((await this.ctx.storage.get("role")) !== "agent") return failure("Unavailable.", 404);
    const url = new URL(request.url);
    const path = url.pathname.slice("/provider".length);
    const route = providerPath(path, request.method, url.search);
    if (!route) return failure("Unsupported managed agent request.", 403);
    if (this.env.OPENAI_MODEL !== "gpt-6-luna" || !this.env.OPENAI_API_KEY) return failure("The analyst is not configured.", 503);
    const {action, sessionId} = route;
    if (await this.ctx.storage.get("image-upgrade") && action === "create")
      return failure("The analyst is updating. No new generation was started.", 409);
    if (sessionId && !await this.ctx.storage.get(`session:${sessionId}`)) return failure("Unknown managed session.", 404);
    const rateAllowed = await this.ctx.storage.transaction(async tx => {
      const now = Date.now();
      let window = await tx.get("provider-window") ?? {time: now, count: 0};
      if (now - window.time > 60000) window = {time: now, count: 0};
      if (++window.count > 120) return false;
      await tx.put("provider-window", window);
      return true;
    });
    if (!rateAllowed) return failure("Please wait before making more model requests.", 429, 60);
    const headers = new Headers({
      Authorization: `Bearer ${this.env.OPENAI_API_KEY}`,
      "Content-Type": "application/json", "OpenAI-Beta": "agents=v1",
      Accept: action === "stream" ? "text/event-stream" : "application/json",
    });
    let bytes;
    if (["create", "input"].includes(action)) {
      const key = request.headers.get("idempotency-key");
      if (!key || !/^[\x21-\x7e]{1,255}$/.test(key)) return failure("A managed input idempotency key is required.", 400);
      try {
        bytes = await limitedBytes(request, PROVIDER_BODY_LIMIT);
        const body = JSON.parse(new TextDecoder().decode(bytes));
        validateProviderBody(body, this.env.OPENAI_MODEL, action);
        if (await this.ctx.storage.get("image-upgrade") && action === "input" &&
            body.events.some(event => event.type !== "agent.session.input.cancel"))
          return failure("The analyst is updating. No continuation was started.", 409);
      } catch (error) { return failure(error.message, 400); }
      // Provider keys are scoped to a visitor even if separate runtimes reuse a key.
      const scopedKey = await hash(`${this.ctx.id.toString()}:${key}`);
      const fingerprint = await hash(`${path}:${new TextDecoder().decode(bytes)}`);
      const accepted = await this.ctx.storage.transaction(async tx => {
        const previous = await tx.get(`idempotency:${scopedKey}`);
        if (previous && previous !== fingerprint) return false;
        await tx.put(`idempotency:${scopedKey}`, fingerprint);
        return true;
      });
      if (!accepted) return failure("This idempotency key belongs to a different managed input.", 409);
      headers.set("Idempotency-Key", scopedKey);
    }
    let response;
    try {
      // Managed turns outlive an HTTP exchange. Do not impose an application
      // deadline or restart a generation after a transport interruption.
      response = await fetch(`https://api.openai.com${path}${url.search}`, {
        method: request.method, headers, body: bytes, redirect: "manual",
      });
    } catch {
      return failure("The managed agent connection was interrupted. Recover the existing session before continuing.", 502);
    }
    if (!response.ok) {
      await response.body?.cancel();
      const status = [400, 401, 403, 404, 409, 422, 429].includes(response.status) ? response.status : 502;
      return failure("The managed agent could not accept this request.", status, status === 429 ? 30 : undefined);
    }
    const inspect = async event => {
      const session = event.session ?? event;
      if (action === "create" && /^sess_[A-Za-z0-9_-]{1,250}$/.test(session.id ?? "")) {
        if (session.agent?.model !== this.env.OPENAI_MODEL || session.environment?.type !== "none" ||
            session.agent.multi_agent?.enabled !== false || session.spend_control != null)
          throw new Error("The managed session configuration changed unexpectedly.");
        await this.ctx.storage.put(`session:${session.id}`, {created: Date.now()});
      }
    };
    const type = response.headers.get("content-type") ?? "application/json";
    if (!type.includes("text/event-stream")) {
      const bytes = await response.arrayBuffer();
      if (bytes.byteLength) await inspect(JSON.parse(new TextDecoder().decode(bytes)));
      return new Response(bytes.byteLength ? bytes : null, {
        status: response.status, headers: {"Content-Type": type, "Cache-Control": "no-store"},
      });
    }
    let pending = "";
    const decoder = new TextDecoder();
    const transform = new TransformStream({
      transform: async (chunk, controller) => {
        pending = (pending + decoder.decode(chunk, {stream: true})).replaceAll("\r\n", "\n");
        let position;
        while ((position = pending.indexOf("\n\n")) >= 0) {
          const block = pending.slice(0, position); pending = pending.slice(position + 2);
          const data = block.split("\n").filter(line => line.startsWith("data:")).map(line => line.slice(5).trimStart()).join("\n");
          let event;
          try { event = JSON.parse(data); } catch { continue; }
          await inspect(event);
        }
        if (pending.length > 4 * 1024 * 1024) throw new Error("A managed event exceeds the transport buffer.");
        // Save session ownership before the runtime receives its creation event
        // and can retrieve, steer, cancel or resume that managed session.
        controller.enqueue(chunk);
      },
    });
    this.ctx.waitUntil(response.body.pipeTo(transform.writable).catch(() => {}));
    return new Response(transform.readable, {headers: {"Content-Type": type, "Cache-Control": "no-store"}});
  }

}

export class ContainerGateway extends WorkerEntrypoint {
  async fetch(request) {
    const url = new URL(request.url);
    const props = this.ctx.props;
    const stub = this.env.ANALYST.get(this.env.ANALYST.idFromString(props.containerId));
    if (url.hostname === "state.internal" && ["/snapshot", "/upgrade-snapshot"].includes(url.pathname) && !url.search && props.role === "agent")
      return stub.fetch(new Request(`https://internal${url.pathname}`, request));
    if (url.hostname === "provider.internal" && props.role === "agent")
      return stub.fetch(new Request(`https://internal/provider${url.pathname}${url.search}`, request));
    // Source data is fetched through HTTP by Python, with an exact source allowlist.
    if (url.protocol === "https:" && url.hostname === "raw.githubusercontent.com" &&
        url.pathname === "/phongtdt/RFID_Data-Analyst_Case-Study-Oct-2026/main/mock-api/defect_records.json" &&
        !url.search && request.method === "GET") {
      const response = await fetch(url, {headers: {Accept: "application/json"}, redirect: "manual"});
      if (response.status >= 300 && response.status < 400) {
        await response.body?.cancel();
        return failure("The configured data source redirected unexpectedly.", 502);
      }
      return response;
    }
    return failure("This network destination is unavailable.");
  }
}
