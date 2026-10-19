import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import {DatabaseSync} from "node:sqlite";
import {gzipSync} from "node:zlib";
import test from "node:test";
import {
  DAY, PROVIDER_BODY_LIMIT, SNAPSHOT_LIMIT, cleanHeaders, providerPath, routeKind, runtimeRecoveryAllowed, setVisitorCookie,
  validOrigin, validateProviderBody, visitorCookie,
} from "../src/policy.js";

class Base {
  constructor(ctx, env) { this.ctx = ctx; this.env = env; }
}
globalThis.__CloudflareTestBase = Base;
globalThis.scheduler = {wait: async () => {}};
const source = (await readFile(new URL("../src/worker.js", import.meta.url), "utf8"))
  .replace('import {DurableObject, WorkerEntrypoint} from "cloudflare:workers";',
    "const DurableObject = globalThis.__CloudflareTestBase; const WorkerEntrypoint = globalThis.__CloudflareTestBase;")
  .replace('from "./policy.js"', `from ${JSON.stringify(new URL("../src/policy.js", import.meta.url).href)}`)
  .replace('from "./container_upgrade.js"', `from ${JSON.stringify(new URL("../src/container_upgrade.js", import.meta.url).href)}`);
const {default: worker, Admission, AnalystContainer, ContainerGateway} = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

function storage() {
  const data = new Map();
  const db = new DatabaseSync(":memory:");
  return {
    data,
    get: async key => structuredClone(data.get(key)),
    put: async (key, value) => { data.set(key, structuredClone(value)); },
    delete: async key => data.delete(key),
    list: async ({prefix}) => new Map([...data].filter(([key]) => key.startsWith(prefix))),
    getAlarm: async () => data.get("alarm") ?? null,
    setAlarm: async value => { data.set("alarm", value); },
    deleteAlarm: async () => { data.delete("alarm"); },
    transaction: async fn => fn(this),
    transactionSync: fn => {
      db.exec("BEGIN");
      try { const result = fn(); db.exec("COMMIT"); return result; }
      catch (error) { db.exec("ROLLBACK"); throw error; }
    },
    sql: {exec(query, ...values) {
      const statement = db.prepare(query);
      const rows = /^(SELECT|PRAGMA)/i.test(query) ? statement.all(...values) : (statement.run(...values), []);
      return {toArray: () => rows};
    }},
  };
}
function context(saved = storage()) {
  saved.transaction = async fn => fn(saved);
  return {storage: saved, id: {toString: () => "1".repeat(64)}, waitUntil: () => {}, exports: {
    ContainerGateway: ({props}) => ({props}),
  }, container: {
    running: false, images: {base: "image:test"},
    inspect: async () => ({image: "image:test"}),
    monitor: async () => {},
    start(options) { this.options = options; this.running = true; },
    setInactivityTimeout: async () => {}, interceptAllOutboundHttp: async () => {}, interceptOutboundHttps: async () => {},
    getTcpPort: () => ({fetch: async () => new Response("ready")}), destroy: async () => {},
  }};
}
const env = () => ({PUBLIC_ORIGIN: "https://tranthiennhan.com", APP_PATH: "/manufacturing-quality", OPENAI_MODEL: "gpt-6-luna", OPENAI_API_KEY: "never-pass-this-to-a-container"});
const call = (path, init = {}) => new Request(`https://internal${path}`, init);
const nativeBody = () => ({
  agent: {model: "gpt-6-luna", instructions: "Analyze case data", tools: [{type: "function", name: "analyze", parameters: {type: "object"}}],
    multi_agent: {enabled: false}, reasoning: {effort: "max", summary: "auto"}, service_tier: "default", text: {}},
  environment: {type: "none"}, input: [{role: "user", content: [{type: "input_text", text: "hello"}]}], stream: true,
});
const mutation = (path, body, key = "input-key") => call(`/provider${path}`, {method: "POST",
  headers: {"Content-Type": "application/json", "Idempotency-Key": key}, body: JSON.stringify(body)});
const create = (body = nativeBody(), key = "create-key") => mutation("/v1/agents/sessions", body, key);
const session = (id = "sess_native") => ({id, agent: {model: "gpt-6-luna", multi_agent: {enabled: false}},
  environment: {type: "none"}, spend_control: null, status: "idle"});

function checkpointArchive(marker) {
  const content = Buffer.from(JSON.stringify({marker}));
  const header = Buffer.alloc(512);
  header.write(".upgrade-handoff.json"); header.write(content.length.toString(8).padStart(11, "0"), 124); header[156] = 48;
  return gzipSync(Buffer.concat([header, content, Buffer.alloc(512 - content.length), Buffer.alloc(1024)]));
}

test("warm previous images stage state before graceful shutdown and start the current image only after final acknowledgement", async () => {
  const ctx = context(); const runtime = new AnalystContainer(ctx, env()); runtime.role = "agent";
  await ctx.storage.put("role", "agent"); await ctx.storage.put("session:sess_unresolved", {retained: true});
  const order = []; const prior = await runtime.snapshot(call("/snapshot", {method: "PUT", headers: {"Content-Type": "application/gzip", "If-None-Match": "*"}, body: "prior-state"}));
  const priorEtag = prior.headers.get("etag");
  let httpIntercept = false, httpsIntercept = false, registrations = 0;
  ctx.container.interceptAllOutboundHttp = async () => { httpIntercept = true; registrations++; };
  ctx.container.interceptOutboundHttps = async () => { httpsIntercept = true; };
  ctx.container.running = true; ctx.container.inspect = async () => ({image: "image:previous"});
  ctx.container.exec = async args => ({output: async () => {
    if (args[5] === "inspect") return {exitCode: 0, stdout: Buffer.from('{"status":"idle"}')};
    assert.equal(ctx.storage.sql.exec("SELECT etag FROM runtime_snapshot WHERE id=1").toArray()[0].etag, priorEtag);
    const staged = await runtime.snapshot(call("/upgrade-snapshot", {method: "PUT", headers: {"Content-Type": "application/gzip", "If-None-Match": "*"}, body: "staged-state"}), true);
    order.push("staged-ack");
    assert.equal(ctx.storage.sql.exec("SELECT etag FROM runtime_snapshot WHERE id=1").toArray()[0].etag, priorEtag);
    order.push("sigterm");
    await runtime.snapshot(call("/snapshot", {method: "PUT", headers: {"Content-Type": "application/gzip", "If-Match": priorEtag}, body: checkpointArchive(args[3])}));
    order.push("final-ack"); ctx.container.running = false;
    httpIntercept = false; httpsIntercept = false;
    return {exitCode: 0, stdout: Buffer.from(JSON.stringify({status: "prepared", staged_etag: staged.headers.get("etag")}))};
  }});
  ctx.container.monitor = async () => { order.push("clean-exit"); };
  ctx.container.start = options => {
    assert.ok(httpIntercept && httpsIntercept, "new instance needs fresh outbound intercepts before TLS trust and restore");
    order.push("start-current"); ctx.container.options = options; ctx.container.running = true; ctx.container.inspect = async () => ({image: options.image});
  };
  await runtime.start();
  assert.deepEqual(order, ["staged-ack", "sigterm", "final-ack", "clean-exit", "start-current"]);
  assert.equal(ctx.container.options.image, "image:test");
  assert.equal(registrations, 2);
  assert.deepEqual(await ctx.storage.get("session:sess_unresolved"), {retained: true});
  assert.equal(await ctx.storage.get("image-upgrade"), undefined);
});

test("a running current image needs no checkpoint, signal or restart", async () => {
  const ctx = context(); ctx.container.running = true;
  ctx.container.exec = async () => { throw new Error("must not execute"); };
  ctx.container.start = () => { throw new Error("must not restart"); };
  const runtime = new AnalystContainer(ctx, env()); runtime.role = "agent";
  await runtime.start();
});

test("a previous dashboard image exits gracefully without accessing a visitor snapshot", async () => {
  const ctx = context(); ctx.container.running = true; ctx.container.inspect = async () => ({image: "image:previous"});
  const order = [];
  ctx.container.signal = signal => { assert.equal(signal, 15); order.push("sigterm"); };
  ctx.container.monitor = async () => { order.push("clean-exit"); ctx.container.running = false; ctx.container.inspect = async () => ({image: "image:test"}); };
  const runtime = new AnalystContainer(ctx, env()); runtime.role = "dashboard";
  await runtime.start();
  assert.deepEqual(order, ["sigterm", "clean-exit"]);
  assert.equal(ctx.container.options.image, "image:test");
});

test("failed upgrade persistence keeps the old runtime and its durable provider records", async () => {
  const ctx = context(); ctx.container.running = true; ctx.container.inspect = async () => ({image: "image:previous"});
  await ctx.storage.put("session:sess_unresolved", {retained: true});
  ctx.container.exec = async args => ({output: async () => ({exitCode: args[5] === "prepare" ? 1 : 0,
    stdout: Buffer.from(JSON.stringify({status: args[5] === "inspect" ? "idle" : args[5] === "release" ? "released" : "failed"}))})});
  ctx.container.monitor = async () => { throw new Error("must not stop"); };
  const runtime = new AnalystContainer(ctx, env()); runtime.role = "agent";
  await assert.rejects(runtime.start());
  assert.equal(ctx.container.running, true);
  assert.deepEqual(await ctx.storage.get("session:sess_unresolved"), {retained: true});
  assert.equal(await ctx.storage.get("image-upgrade"), undefined);
});

test("an active previous run retains reads and cancellation while new prompts wait for its upgrade", async () => {
  const ctx = context(); ctx.container.running = true; ctx.container.inspect = async () => ({image: "image:previous"});
  ctx.container.exec = async () => ({output: async () => ({exitCode: 0, stdout: Buffer.from('{"status":"busy"}')})});
  const runtime = new AnalystContainer(ctx, env());
  for (const [method, path, expected] of [["GET", "/api/sessions/s1", 200], ["POST", "/api/sessions/s1/cancel", 200], ["POST", "/api/sessions/s1/prompt", 503]]) {
    const response = await runtime.fetch(new Request(`https://tranthiennhan.com${path}`, {method, headers: {"x-analyst-role": "agent"}}));
    assert.equal(response.status, expected);
  }
  assert.equal((await runtime.fetch(new Request("https://tranthiennhan.com/api/sessions/s1/ws", {
    headers: {"x-analyst-role": "agent", Upgrade: "websocket"},
  }))).status, 503);
  assert.equal(await ctx.storage.get("image-upgrade"), undefined);
});

test("a stopped handoff without a final marker acknowledgement cannot start a fresh paid runtime", async () => {
  const ctx = context(); await ctx.storage.put("image-upgrade", {marker: "pending"});
  ctx.container.start = () => { throw new Error("must not restart without proof"); };
  const runtime = new AnalystContainer(ctx, env()); runtime.role = "agent";
  await assert.rejects(runtime.start(), /reconciliation/);
});

test("a recreated Durable Object finishes an acknowledged clean handoff without replaying the old process", async () => {
  const ctx = context(); await ctx.storage.put("role", "agent");
  await ctx.storage.put("image-upgrade", {marker: "accepted-marker", image: "image:test"});
  await ctx.storage.put("image-upgrade-exit", {marker: "accepted-marker", clean: true});
  const runtime = new AnalystContainer(ctx, env()); runtime.role = "agent";
  await runtime.snapshot(call("/snapshot", {method: "PUT", headers: {"Content-Type": "application/gzip", "If-None-Match": "*"}, body: checkpointArchive("accepted-marker")}));
  ctx.container.monitor = async () => { throw new Error("New Durable Object has no previous monitor handle"); };
  ctx.container.exec = async () => { throw new Error("must not replay old handoff"); };
  await runtime.start();
  assert.equal(ctx.container.options.image, "image:test");
  assert.equal(await ctx.storage.get("image-upgrade"), undefined);
});

test("image upgrade admission blocks new paid requests and retains cancellation and retrieval", async () => {
  const ctx = context(); await ctx.storage.put("role", "agent");
  await ctx.storage.put("session:sess_native", {owned: true}); await ctx.storage.put("image-upgrade", {marker: "pending"});
  const runtime = new AnalystContainer(ctx, env());
  const originalFetch = globalThis.fetch; let forwarded = 0;
  globalThis.fetch = async () => { forwarded++; return new Response(null, {status: 202}); };
  try {
    assert.equal((await runtime.provider(create())).status, 409);
    assert.equal((await runtime.provider(mutation("/v1/agents/sessions/sess_native/events", {events: [{type: "agent.session.input.message", input: "new message"}]}))).status, 409);
    assert.equal(forwarded, 0);
    assert.equal((await runtime.provider(mutation("/v1/agents/sessions/sess_native/events", {events: [{type: "agent.session.input.cancel"}]}))).status, 202);
    assert.equal(forwarded, 1);
  } finally { globalThis.fetch = originalFetch; }
});

test("cookies are opaque, secure and scoped to the application", () => {
  const token = "a".repeat(64);
  assert.equal(visitorCookie(new Request("https://site", {headers: {cookie: `other=1; mq_visitor=${token}`}})), token);
  assert.equal(visitorCookie(new Request("https://site", {headers: {cookie: "mq_visitor=visitor-id"}})), null);
  const cookie = setVisitorCookie(token, "/manufacturing-quality");
  for (const flag of ["HttpOnly", "Secure", "SameSite=Strict", "Path=/manufacturing-quality"])
    assert.ok(cookie.includes(flag));
});

test("mutation and websocket requests require the exact public origin", () => {
  for (const origin of [undefined, "https://evil.example", "https://www.tranthiennhan.com"])
    assert.equal(validOrigin(new Request("https://tranthiennhan.com", {method: "POST", headers: origin ? {origin} : {}}), "https://tranthiennhan.com"), false);
  assert.ok(validOrigin(new Request("https://tranthiennhan.com", {method: "POST", headers: {origin: "https://tranthiennhan.com"}}), "https://tranthiennhan.com"));
  assert.equal(routeKind("/api/operations/cases", "GET"), "blocked");
  assert.equal(routeKind("/_internal/snapshot", "PUT"), "blocked");
  assert.equal(routeKind("/api/webhooks/openai", "POST"), "blocked");
});

test("external links open the public page and assets while cross-site API requests stay blocked", async () => {
  const settings = env();
  const gate = new Admission(context(), settings);
  settings.ADMISSION = {getByName: () => ({fetch: request => gate.fetch(request)})};
  settings.ASSETS = {fetch: async () => new Response("public page")};
  const publicRequest = (path, more = {}) => new Request(`https://tranthiennhan.com${path}`, {
    headers: {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document", ...more},
  });
  const response = await worker.fetch(publicRequest("/"), settings);
  assert.equal(response.status, 200);
  assert.equal(await response.text(), "public page");
  assert.ok(response.headers.get("set-cookie").includes("mq_visitor="));
  assert.equal((await worker.fetch(publicRequest("/assets/app.js", {"Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "script"}), settings)).status, 200);
  assert.equal((await worker.fetch(publicRequest("/api/dashboard"), settings)).status, 403);
  assert.equal((await worker.fetch(new Request("https://tranthiennhan.com/api/sessions", {
    method: "POST", headers: {origin: "https://evil.example", "Sec-Fetch-Site": "cross-site"},
  }), settings)).status, 403);
  assert.equal((await worker.fetch(new Request("https://tranthiennhan.com/", {
    headers: {origin: "https://evil.example", upgrade: "websocket", "Sec-Fetch-Site": "cross-site"},
  }), settings)).status, 403);
});

test("client credentials never reach the runtime or model provider", () => {
  const headers = cleanHeaders({authorization: "Bearer visitor", cookie: "mq_visitor=x", "x-api-key": "x", "x-analyst-role": "agent", accept: "application/json"});
  assert.equal(headers.get("authorization"), null);
  assert.equal(headers.get("cookie"), null);
  assert.equal(headers.get("x-api-key"), null);
  assert.equal(headers.get("x-analyst-role"), null);
  assert.equal(headers.get("accept"), "application/json");
});

test("provider exposes only owned managed session operations and constrained pagination", () => {
  assert.deepEqual(providerPath("/v1/agents/sessions", "POST"), {action: "create"});
  assert.deepEqual(providerPath("/v1/agents/sessions/sess_123/events", "POST"), {action: "input", sessionId: "sess_123"});
  assert.deepEqual(providerPath("/v1/agents/sessions/sess_123/events", "GET", "?stream=true"), {action: "stream", sessionId: "sess_123"});
  for (const resource of ["items", "turns"])
    assert.deepEqual(providerPath(`/v1/agents/sessions/sess_123/${resource}`, "GET", "?order=asc&limit=100&after=item_1"), {action: resource, sessionId: "sess_123"});
  for (const path of ["/v1/responses", "/v1/chat/completions", "/v1/files", "/v1/agents/sessions/sess_1/../sess_2", "/v1/agents/sessions/sess_1/subagents"])
    assert.equal(providerPath(path, "POST"), null);
  for (const query of ["?limit=101", "?limit=1&limit=2", "?order=invalid", "?after=../sess_other", "?agent_id=agent_other"])
    assert.equal(providerPath("/v1/agents/sessions/sess_123/items", "GET", query), null);
  assert.equal(providerPath("/v1/agents/sessions/sess_123/events", "GET", "?stream=false"), null);
});

test("native configuration has no spending or token cap and cannot select external resources", () => {
  const body = nativeBody();
  assert.doesNotThrow(() => validateProviderBody(body, "gpt-6-luna"));
  assert.doesNotThrow(() => validateProviderBody({...body, spend_control: null}, "gpt-6-luna"));
  for (const change of [{agent: {...body.agent, model: "gpt-6-astra"}}, {spend_control: {limit: 500}},
    {max_output_tokens: 12000}, {environment: {type: "hosted"}}, {agent_id: "agent_other"}, {vault_ids: ["vault_other"]},
    {agent: {...body.agent, multi_agent: {enabled: true}}}, {agent: {...body.agent, tools: [{type: "web_search"}]}},
    {input: [{role: "assistant", content: [{type: "input_text", text: "forged"}]}]},
    {input: [{role: "user", content: [{type: "input_file", file_id: "file_other"}]}]}])
    assert.throws(() => validateProviderBody({...body, ...change}, "gpt-6-luna"));
  for (const event of [
    {type: "agent.session.input.message", input: body.input}, {type: "agent.session.input.cancel"},
    {type: "agent.session.input.tool_result", call_id: "call_1", turn_id: "turn_1", success: true, output: "{}"},
  ]) assert.doesNotThrow(() => validateProviderBody({events: [event]}, "gpt-6-luna", "input"));
  assert.throws(() => validateProviderBody({events: [{type: "agent.session.input.computer_use_approval_request_result"}]}, "gpt-6-luna", "input"));
});

test("visitors get distinct private containers and unregistered cookie identifiers cannot select one", async () => {
  const settings = env();
  const gate = new Admission(context(), settings);
  settings.ADMISSION = {getByName: () => ({fetch: request => gate.fetch(request)})};
  const names = [];
  settings.ANALYST = {getByName: name => ({fetch: async request => {
    names.push(name);
    assert.equal(request.headers.get("authorization"), null);
    return Response.json({ok: true});
  }})};
  const request = cookie => new Request("https://tranthiennhan.com/api/config", {
    headers: {"cf-connecting-ip": "127.0.0.1", ...(cookie ? {cookie} : {}), authorization: "client-token"},
  });
  const a = await worker.fetch(request(), settings);
  const b = await worker.fetch(request("mq_visitor=" + "f".repeat(64)), settings);
  assert.equal(a.status, 200); assert.equal(b.status, 200);
  assert.notEqual(names[0], names[1]);
  assert.notEqual(names[1], "visitor:" + "f".repeat(64));
  await worker.fetch(request(a.headers.get("set-cookie").split(";")[0]), settings);
  assert.equal(names[0], names[2]);
  const cookie = a.headers.get("set-cookie").split(";")[0];
  for (const path of ["/api/dashboard", "/api/dashboard/contexts/example", "/api/sessions/example"])
    await worker.fetch(new Request(`https://tranthiennhan.com${path}`, {headers: {cookie}}), settings);
  assert.deepEqual(names.slice(2), Array(4).fill(names[0]));
});

test("runtime receives the validated public origin for independent backend validation", async () => {
  const ctx = context();
  let forwarded;
  ctx.container.getTcpPort = () => ({fetch: async request => {
    if (typeof request === "string") return new Response("ready");
    forwarded = request;
    return Response.json({ok: true});
  }});
  const runtime = new AnalystContainer(ctx, env());
  const response = await runtime.fetch(new Request("https://tranthiennhan.com/api/sessions", {
    method: "POST", headers: {origin: "https://tranthiennhan.com", "x-analyst-role": "agent"},
  }));
  assert.equal(response.status, 200);
  assert.equal(forwarded.headers.get("origin"), "https://tranthiennhan.com");
});

test("container startup passes no provider secret and denies arbitrary outbound hosts", async () => {
  const ctx = context(); const runtime = new AnalystContainer(ctx, env()); runtime.role = "agent";
  await runtime.start();
  assert.equal(ctx.container.options.env.OPENAI_API_KEY, undefined);
  assert.equal(ctx.container.options.enableInternet, false);
  assert.equal(ctx.container.options.env.ANALYST_STATE_URL, "http://state.internal/snapshot");
  const gateway = new ContainerGateway({props: {role: "agent", containerId: "1".repeat(64)}}, {
    ANALYST: {idFromString: id => id, get: () => ({fetch: async () => new Response("private")})},
  });
  assert.equal((await gateway.fetch(new Request("http://state.internal/snapshot"))).status, 200);
  assert.equal((await gateway.fetch(new Request("https://api.openai.com/v1/responses"))).status, 403);
  assert.equal((await gateway.fetch(new Request("https://attacker.example"))).status, 403);
  assert.equal((await gateway.fetch(new Request("http://state.internal/snapshot?other=visitor"))).status, 403);
});

test("outbound source and provider use Workers-compatible manual redirects and reject redirects", async () => {
  const settings = env();
  const ctx = context(); await ctx.storage.put("role", "agent");
  settings.ADMISSION = {getByName: () => ({fetch: async () => Response.json({ok: true})})};
  settings.ANALYST = {idFromString: id => id, get: () => ({fetch: async () => new Response("private")})};
  const gateway = new ContainerGateway({props: {role: "agent", containerId: "1".repeat(64)}}, settings);
  const originalFetch = globalThis.fetch;
  const destinations = [];
  let sourceRedirect = false;
  globalThis.fetch = async (destination, options) => {
    destinations.push(String(destination));
    assert.equal(options.redirect, "manual");
    if (String(destination).startsWith("https://raw.githubusercontent.com"))
      return sourceRedirect ? new Response("redirect", {status: 302, headers: {Location: "https://unexpected.example"}})
        : Response.json({data: "source"});
    return new Response("redirect", {status: 307, headers: {Location: "https://unexpected.example"}});
  };
  try {
    const source = () => new Request("https://raw.githubusercontent.com/phongtdt/RFID_Data-Analyst_Case-Study-Oct-2026/main/mock-api/defect_records.json");
    assert.deepEqual(await (await gateway.fetch(source())).json(), {data: "source"});
    sourceRedirect = true;
    const rejectedSource = await gateway.fetch(source());
    assert.equal(rejectedSource.status, 502);
    assert.equal(rejectedSource.headers.get("Location"), null);
    const runtime = new AnalystContainer(ctx, settings);
    const rejectedProvider = await runtime.provider(create());
    assert.equal(rejectedProvider.status, 502);
    assert.equal(rejectedProvider.headers.get("Location"), null);
    assert.equal(destinations.length, 3);
    assert.ok(destinations.every(value => !value.includes("unexpected.example")));
  } finally { globalThis.fetch = originalFetch; }
});

test("snapshot writes use atomic CAS and survive a Durable Object restart", async () => {
  const ctx = context(); await ctx.storage.put("role", "agent");
  const runtime = new AnalystContainer(ctx, env());
  assert.equal((await runtime.snapshot(call("/snapshot"))).status, 404);
  const put = (bytes, conditions) => call("/snapshot", {method: "PUT", headers: {"Content-Type": "application/gzip", ...conditions}, body: bytes});
  const bytes = new Uint8Array([31, 139, 1, 2, 3]);
  const initial = await runtime.snapshot(put(bytes, {"If-None-Match": "*"}));
  assert.equal(initial.status, 204);
  assert.equal((await runtime.snapshot(put(bytes, {"If-None-Match": "*"}))).status, 412);
  assert.equal((await runtime.snapshot(put(bytes, {"If-Match": '"stale"'}))).status, 412);
  const resumed = new AnalystContainer(context(ctx.storage), env());
  const read = await resumed.snapshot(call("/snapshot"));
  assert.equal(read.headers.get("ETag"), initial.headers.get("ETag"));
  assert.deepEqual(new Uint8Array(await read.arrayBuffer()), bytes);
  assert.equal((await resumed.snapshot(put(bytes, {"If-Match": initial.headers.get("ETag")}))).status, 204);
  assert.equal((await resumed.snapshot(put(bytes, {"Content-Length": String(SNAPSHOT_LIMIT + 1), "If-Match": initial.headers.get("ETag")}))).status, 413);
});

test("private active-work heartbeat does not read or mutate a workspace archive", async () => {
  const ctx = context(); await ctx.storage.put("role", "agent");
  const runtime = new AnalystContainer(ctx, env());
  const before = [...ctx.storage.data];
  ctx.storage.sql.exec = () => { throw new Error("A heartbeat must not query the snapshot"); };
  const response = await runtime.snapshot(call("/snapshot", {method: "HEAD"}));
  assert.equal(response.status, 204); assert.equal(await response.text(), "");
  assert.deepEqual([...ctx.storage.data], before);
  await ctx.storage.put("role", "dashboard");
  assert.equal((await runtime.snapshot(call("/snapshot", {method: "HEAD"}))).status, 404);
});

test("session ownership survives restart and rejects access from a different visitor", async () => {
  const ctx = context(); await ctx.storage.put("role", "agent");
  const oldFetch = globalThis.fetch;
  globalThis.fetch = async () => Response.json(session());
  try {
    const runtime = new AnalystContainer(ctx, env());
    assert.equal((await runtime.provider(create())).status, 200);
    const resumed = new AnalystContainer(context(ctx.storage), env());
    assert.equal((await resumed.provider(call("/provider/v1/agents/sessions/sess_native"))).status, 200);
    const other = context(); await other.storage.put("role", "agent");
    const stranger = new AnalystContainer(other, env());
    for (const suffix of ["", "/events", "/items", "/turns"])
      assert.equal((await stranger.provider(call(`/provider/v1/agents/sessions/sess_native${suffix}`))).status, 404);
    assert.equal((await stranger.provider(mutation("/v1/agents/sessions/sess_native/events", {events: [{type: "agent.session.input.cancel"}]}))).status, 404);
  } finally { globalThis.fetch = oldFetch; }
});

test("stream creation reaches Python only after durable session ownership writes", async () => {
  const ctx = context(); await ctx.storage.put("role", "agent");
  const originalPut = ctx.storage.put;
  let release;
  const blocked = new Promise(resolve => { release = resolve; });
  ctx.storage.put = async (key, value) => {
    if (key === "session:sess_native") await blocked;
    return originalPut(key, value);
  };
  const oldFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(new ReadableStream({start(controller) {
    const encode = value => new TextEncoder().encode(`data: ${JSON.stringify(value)}\n\n`);
    controller.enqueue(encode({type: "agent.session.created", session: session()}));
    controller.enqueue(encode({type: "agent.session.turn.completed", session_id: "sess_native", turn_id: "turn_1"}));
    controller.close();
  }}), {headers: {"Content-Type": "text/event-stream"}});
  try {
    const result = await new AnalystContainer(ctx, env()).provider(create());
    const reader = result.body.getReader();
    let visible = false;
    const first = reader.read().then(value => { visible = true; return value; });
    await new Promise(resolve => setTimeout(resolve, 15));
    assert.equal(visible, false);
    release();
    assert.ok(new TextDecoder().decode((await first).value).includes("agent.session.created"));
    assert.ok(await ctx.storage.get("session:sess_native"));
    await reader.read(); await reader.read();
  } finally { globalThis.fetch = oldFetch; release(); }
});

test("a provider configuration mismatch cannot grant session ownership or expose its stream", async () => {
  const ctx = context(); await ctx.storage.put("role", "agent");
  const oldFetch = globalThis.fetch;
  const changed = session(); changed.agent.model = "gpt-6-astra";
  globalThis.fetch = async () => new Response(`data: ${JSON.stringify({type: "agent.session.created", session: changed})}\n\n`,
    {headers: {"Content-Type": "text/event-stream"}});
  try {
    const response = await new AnalystContainer(ctx, env()).provider(create());
    await assert.rejects(response.text(), /configuration changed/);
    assert.equal(await ctx.storage.get("session:sess_native"), undefined);
  } finally { globalThis.fetch = oldFetch; }
});

test("native mutations forward scoped idempotency, reject key conflicts and preserve empty 202 responses", async () => {
  const ctx = context(); await ctx.storage.put("role", "agent");
  const oldFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({url, options});
    assert.equal(options.headers.get("OpenAI-Beta"), "agents=v1");
    assert.equal(options.headers.get("Authorization"), `Bearer ${env().OPENAI_API_KEY}`);
    assert.equal(options.redirect, "manual");
    assert.equal(options.signal, undefined);
    return url.endsWith("/events") ? new Response(null, {status: 202}) : Response.json(session());
  };
  try {
    const runtime = new AnalystContainer(ctx, env());
    assert.equal((await runtime.provider(create())).status, 200);
    const resumed = new AnalystContainer(context(ctx.storage), env());
    assert.equal((await resumed.provider(create())).status, 200);
    const key = calls[0].options.headers.get("Idempotency-Key");
    assert.match(key, /^[a-f0-9]{64}$/);
    assert.equal(calls[1].options.headers.get("Idempotency-Key"), key);
    const body = nativeBody(); body.input[0].content[0].text = "different";
    assert.equal((await resumed.provider(create(body))).status, 409);
    const accepted = await resumed.provider(mutation("/v1/agents/sessions/sess_native/events", {events: [{type: "agent.session.input.cancel"}]}));
    assert.equal(accepted.status, 202); assert.equal(await accepted.text(), "");
    const other = context(); other.id = {toString: () => "2".repeat(64)}; await other.storage.put("role", "agent");
    assert.equal((await new AnalystContainer(other, env()).provider(create())).status, 200);
    assert.notEqual(calls.at(-1).options.headers.get("Idempotency-Key"), key);
    assert.equal(calls.length, 4);
  } finally { globalThis.fetch = oldFetch; }
});

test("provider streaming has no total output cap and collection query parameters are forwarded", async () => {
  const ctx = context(); await ctx.storage.put("role", "agent"); await ctx.storage.put("session:sess_native", {});
  const oldFetch = globalThis.fetch;
  const event = `data: ${JSON.stringify({type: "agent.session.output_text.delta", delta: "x".repeat(512 * 1024)})}\n\n`;
  globalThis.fetch = async url => {
    if (url.includes("/items")) {
      assert.ok(url.endsWith("?order=asc&limit=100&after=item_1"));
      return Response.json({data: [], has_more: false});
    }
    assert.ok(url.endsWith("/events?stream=true"));
    return new Response(new ReadableStream({start(controller) {
      for (let i = 0; i < 9; i++) controller.enqueue(new TextEncoder().encode(event));
      controller.close();
    }}), {headers: {"Content-Type": "text/event-stream"}});
  };
  try {
    const runtime = new AnalystContainer(ctx, env());
    const result = await runtime.provider(call("/provider/v1/agents/sessions/sess_native/events?stream=true"));
    assert.equal((await result.arrayBuffer()).byteLength, new TextEncoder().encode(event).length * 9);
    assert.equal((await runtime.provider(call("/provider/v1/agents/sessions/sess_native/items?order=asc&limit=100&after=item_1"))).status, 200);
  } finally { globalThis.fetch = oldFetch; }
});

test("the managed bridge retains infrastructure boundaries without spending or lifetime gates", async () => {
  const ctx = context(); await ctx.storage.put("role", "agent");
  await ctx.storage.put("provider-ledger", [{id: "historical", amount: 1000, active: true}]);
  await ctx.storage.put("alarm", Date.now() + 1000);
  const runtime = new AnalystContainer(ctx, env());
  await runtime.fetch(new Request("https://tranthiennhan.com/api/config", {headers: {"x-analyst-role": "agent"}}));
  assert.equal(await ctx.storage.get("alarm"), undefined);
  assert.equal(runtime.alarm, undefined);
  const oldFetch = globalThis.fetch;
  globalThis.fetch = async () => Response.json(session());
  try {
    assert.equal((await runtime.provider(create())).status, 200);
    assert.deepEqual(await ctx.storage.get("provider-ledger"), [{id: "historical", amount: 1000, active: true}]);
    const noKey = call("/provider/v1/agents/sessions", {method: "POST", body: JSON.stringify(nativeBody())});
    assert.equal((await runtime.provider(noKey)).status, 400);
    const oversize = call("/provider/v1/agents/sessions", {method: "POST", headers: {"Idempotency-Key": "large", "Content-Length": String(PROVIDER_BODY_LIMIT + 1)}, body: "{}"});
    assert.equal((await runtime.provider(oversize)).status, 400);
    assert.equal((await runtime.provider(call("/provider/v1/responses", {method: "POST", body: "{}"}))).status, 403);
  } finally { globalThis.fetch = oldFetch; }
});

test("IP admission survives cookie replacement", async () => {
  const ctx = context(); const gate = new Admission(ctx, env());
  const post = body => call("/admit", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  for (let i = 0; i < 10; i++)
    assert.equal((await gate.fetch(post({ip: "same-connection", token: "f".repeat(64), agent: false}))).status, 200);
  assert.equal((await gate.fetch(post({ip: "same-connection", token: null, agent: false}))).status, 429);
});

test("expired capacity leases retain existing visitor reads and cancellation without admitting new work", async t => {
  let now = 1_000_000_000;
  t.mock.method(Date, "now", () => now);
  const ctx = context(); const settings = env(); const gate = new Admission(ctx, settings);
  settings.ADMISSION = {getByName: () => ({fetch: request => gate.fetch(request)})};
  const forwarded = [];
  settings.ANALYST = {getByName: name => ({fetch: async request => {
    forwarded.push({name, path: new URL(request.url).pathname, method: request.method});
    return Response.json({ok: true});
  }})};
  const request = (path, {ip = "original", cookie, method = "GET", websocket = false} = {}) => new Request(
    `https://tranthiennhan.com${path}`, {method, headers: {
      Origin: settings.PUBLIC_ORIGIN, "cf-connecting-ip": ip,
      ...(cookie ? {Cookie: cookie} : {}), ...(websocket ? {Upgrade: "websocket"} : {}),
    }},
  );
  const first = await worker.fetch(request("/api/sessions", {method: "POST"}), settings);
  assert.equal(first.status, 200);
  const cookie = first.headers.get("set-cookie").split(";")[0];
  const originalContainer = forwarded[0].name;
  now += 31 * 60 * 1000;
  for (let index = 0; index < 10; index++) {
    const response = await worker.fetch(request("/api/sessions", {method: "POST", ip: `new-${index}`}), settings);
    assert.equal(response.status, 200);
  }
  const fullCapacity = await ctx.storage.get("active");
  assert.equal(fullCapacity.length, 10);
  for (const path of ["/api/config", "/api/dashboard", "/api/sessions/s1", "/api/sessions/s1/prompt-requests/request-1"]) {
    const response = await worker.fetch(request(path, {cookie}), settings);
    assert.equal(response.status, 200, path);
    assert.equal(response.headers.get("set-cookie"), null);
    assert.equal(forwarded.at(-1).name, originalContainer);
  }
  assert.equal((await worker.fetch(request("/api/sessions/s1/cancel", {cookie, method: "POST"}), settings)).status, 200);
  assert.equal(forwarded.at(-1).name, originalContainer);
  const callsAfterRecovery = forwarded.length;
  for (const path of ["/api/sessions", "/api/sessions/s1/prompt", "/api/sessions/s1/files"]) {
    assert.equal((await worker.fetch(request(path, {cookie, method: "POST"}), settings)).status, 503, path);
  }
  assert.equal((await worker.fetch(request("/api/sessions/s1/ws", {cookie, websocket: true}), settings)).status, 503);
  for (const unknown of [undefined, "mq_visitor=" + "f".repeat(64)]) {
    assert.equal((await worker.fetch(request("/api/sessions/s1", {cookie: unknown, ip: "unknown"}), settings)).status, 503);
    assert.equal((await worker.fetch(request("/api/sessions/s1/cancel", {cookie: unknown, ip: "unknown", method: "POST"}), settings)).status, 503);
  }
  assert.equal(forwarded.length, callsAfterRecovery, "blocked requests never reach a visitor container");
  assert.deepEqual(await ctx.storage.get("active"), fullCapacity, "recovery does not grant or extend an execution lease");
  assert.equal(runtimeRecoveryAllowed(request("/api/sessions/s1/ws", {websocket: true})), false);
  const token = cookie.slice("mq_visitor=".length);
  await ctx.storage.put(`visitor:${token}`, {...await ctx.storage.get(`visitor:${token}`), expires: now - 1});
  assert.equal((await worker.fetch(request("/api/sessions/s1/cancel", {cookie, method: "POST"}), settings)).status, 503);
});

test("admission preserves the earliest cleanup alarm and removes expired identifiers when it fires", async t => {
  let now = 2_000_000_000;
  t.mock.method(Date, "now", () => now);
  const ctx = context(); const gate = new Admission(ctx, env());
  const post = (ip, token = null) => call("/admit", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ip, token, agent: false})});
  const initial = await (await gate.fetch(post("old-ip"))).json();
  const firstAlarm = await ctx.storage.getAlarm();
  assert.equal(firstAlarm, now + DAY);
  await ctx.storage.put("visitor:expired", {expires: firstAlarm - 1});
  for (let hour = 1; hour <= 23; hour++) {
    now = firstAlarm - DAY + hour * 60 * 60 * 1000;
    assert.equal((await gate.fetch(post("current-ip", initial.token))).status, 200);
    assert.equal(await ctx.storage.getAlarm(), firstAlarm);
  }
  const restarted = new Admission(context(ctx.storage), env());
  now = firstAlarm + 1;
  await restarted.alarm();
  assert.equal(await ctx.storage.get("ip:old-ip"), undefined);
  assert.equal(await ctx.storage.get("visitor:expired"), undefined);
  assert.ok(await ctx.storage.get("ip:current-ip"));
  assert.ok(await ctx.storage.get(`visitor:${initial.token}`));
  assert.equal(await ctx.storage.getAlarm(), now + DAY);
  const earlier = now + 1000;
  await ctx.storage.setAlarm(earlier);
  await restarted.fetch(post("current-ip", initial.token));
  assert.equal(await ctx.storage.getAlarm(), earlier);
});

test("an ambiguous managed creation is never retried by the bridge and preserves its idempotency binding", async () => {
  const ctx = context(); await ctx.storage.put("role", "agent");
  const oldFetch = globalThis.fetch;
  const keys = [];
  globalThis.fetch = async (url, options) => { keys.push(options.headers.get("Idempotency-Key")); throw new Error("connection lost after sending"); };
  try {
    assert.equal((await new AnalystContainer(ctx, env()).provider(create())).status, 502);
    assert.equal(keys.length, 1);
    assert.equal((await new AnalystContainer(context(ctx.storage), env()).provider(create())).status, 502);
    assert.equal(keys.length, 2); assert.equal(keys[0], keys[1]);
    const other = nativeBody(); other.input[0].content[0].text = "different";
    assert.equal((await new AnalystContainer(ctx, env()).provider(create(other))).status, 409);
    assert.equal(keys.length, 2);
  } finally { globalThis.fetch = oldFetch; }
});
