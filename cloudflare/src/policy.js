export const COOKIE = "mq_visitor";
export const SNAPSHOT_LIMIT = 32 * 1024 * 1024;
export const DAY = 24 * 60 * 60 * 1000;
// Native managed sessions have a 4 MiB input/schema transport boundary.
export const PROVIDER_BODY_LIMIT = 4 * 1024 * 1024;

export function failure(message, status = 403, retryAfter) {
  return Response.json({detail: message}, {
    status,
    headers: {"Cache-Control": "no-store", ...(retryAfter ? {"Retry-After": String(retryAfter)} : {})},
  });
}

export function visitorCookie(request) {
  const value = (request.headers.get("cookie") ?? "").split(";")
    .map(item => item.trim()).find(item => item.startsWith(`${COOKIE}=`))?.slice(COOKIE.length + 1);
  return /^[a-f0-9]{64}$/.test(value ?? "") ? value : null;
}

export function setVisitorCookie(value, basePath) {
  return `${COOKIE}=${value}; Path=${basePath}; HttpOnly; Secure; SameSite=Strict; Max-Age=604800`;
}

export function validOrigin(request, origin, publicStatic = false) {
  const source = request.headers.get("origin");
  if (["POST", "PUT", "PATCH", "DELETE"].includes(request.method) ||
      request.headers.get("upgrade")?.toLowerCase() === "websocket") return source === origin;
  if (publicStatic && ["GET", "HEAD"].includes(request.method)) return true;
  if (source && source !== origin) return false;
  return !["cross-site", "same-site"].includes(request.headers.get("sec-fetch-site"));
}

export function routeKind(path, method) {
  if (path.startsWith("/_internal") || path.startsWith("/api/operations") ||
      path === "/api/webhooks/openai") return "blocked";
  if (path === "/api/config" || path.startsWith("/api/sessions")) return "agent";
  if (path.startsWith("/api/dashboard")) {
    return ["GET", "HEAD"].includes(method) || (method === "POST" &&
      (path === "/api/dashboard/refresh" || /^\/api\/dashboard\/contexts\/[^/]+\/compare-periods$/.test(path)))
      ? "agent" : "blocked";
  }
  if (path === "/health" || path === "/" || path.startsWith("/assets/") ||
      path === "/favicon.svg") {
    return ["GET", "HEAD"].includes(method) ? "dashboard" : "blocked";
  }
  return "blocked";
}

const identifier = value => typeof value === "string" && /^[A-Za-z0-9_-]{1,256}$/.test(value);

export function providerPath(path, method, search = "") {
  let route;
  if (path === "/v1/agents/sessions" && method === "POST") route = {action: "create"};
  const match = /^\/v1\/agents\/sessions\/(sess_[A-Za-z0-9_-]{1,250})(?:\/(events|items|turns))?$/.exec(path);
  if (match) {
    const [, sessionId, resource] = match;
    if (!resource && method === "GET") route = {action: "retrieve", sessionId};
    if (resource === "events" && ["GET", "POST"].includes(method))
      route = {action: method === "GET" ? "stream" : "input", sessionId};
    if (["items", "turns"].includes(resource) && method === "GET") route = {action: resource, sessionId};
  }
  if (!route) return null;
  const query = new URLSearchParams(search);
  const allowed = ["items", "turns"].includes(route.action) ? ["order", "limit", "after"]
    : route.action === "stream" ? ["stream"] : [];
  for (const key of query.keys()) {
    if (!allowed.includes(key) || query.getAll(key).length !== 1) return null;
    const value = query.get(key);
    if (key === "order" && !["asc", "desc"].includes(value)) return null;
    if (key === "limit" && (!/^[1-9][0-9]*$/.test(value) || Number(value) > 100)) return null;
    if (key === "after" && !identifier(value)) return null;
    if (key === "stream" && value !== "true") return null;
  }
  return route;
}

function fields(value, allowed) {
  if (!value || typeof value !== "object" || Array.isArray(value) || Object.keys(value).some(key => !allowed.includes(key)))
    throw new Error("Unsupported managed agent configuration.");
}

function messages(input) {
  if (typeof input === "string" && input.length) return;
  if (!Array.isArray(input) || !input.length) throw new Error("A user message is required.");
  for (const message of input) {
    fields(message, ["role", "content"]);
    if (message.role !== "user" || !Array.isArray(message.content) || !message.content.length)
      throw new Error("The analyst accepts user text and locally parsed files only.");
    for (const part of message.content) {
      fields(part, ["type", "text"]);
      if (part.type !== "input_text" || typeof part.text !== "string")
        throw new Error("The analyst accepts user text and locally parsed files only.");
    }
  }
}

export function validateProviderBody(body, model, action = "create") {
  if (action === "input") {
    fields(body, ["events"]);
    if (!Array.isArray(body.events) || !body.events.length) throw new Error("Managed input events are required.");
    for (const event of body.events) {
      if (event?.type === "agent.session.input.message") {
        fields(event, ["type", "input"]); messages(event.input);
      } else if (event?.type === "agent.session.input.cancel") {
        fields(event, ["type"]);
      } else if (event?.type === "agent.session.input.tool_result") {
        fields(event, ["type", "call_id", "turn_id", "success", "output", "error"]);
        if (!identifier(event.call_id) || !identifier(event.turn_id) || typeof event.success !== "boolean" ||
            (event.output !== undefined && typeof event.output !== "string") ||
            (event.error !== undefined && typeof event.error !== "string")) throw new Error("Invalid local tool result.");
      } else throw new Error("Unsupported managed input event.");
    }
    return;
  }
  fields(body, ["agent", "environment", "input", "stream", "spend_control"]);
  fields(body.agent, ["model", "instructions", "tools", "multi_agent", "reasoning", "service_tier", "text"]);
  if (model !== "gpt-6-luna" || body.agent.model !== model) throw new Error("This application supports gpt-6-luna only.");
  fields(body.environment, ["type"]);
  if (body.environment.type !== "none") throw new Error("Execution uses the isolated local analysis tools.");
  fields(body.agent.multi_agent, ["enabled"]);
  if (body.agent.multi_agent.enabled !== false) throw new Error("Native subagents are unavailable in this workspace.");
  if (body.spend_control !== undefined && body.spend_control !== null) throw new Error("Managed sessions run without a spending limit.");
  if (body.stream !== undefined && typeof body.stream !== "boolean") throw new Error("Invalid stream option.");
  if (![undefined, "default", "auto", "flex", "priority", "fast"].includes(body.agent.service_tier)) throw new Error("Unsupported service tier.");
  if (body.agent.instructions !== undefined && typeof body.agent.instructions !== "string") throw new Error("Invalid agent instructions.");
  if (body.agent.tools !== undefined && (!Array.isArray(body.agent.tools) || body.agent.tools.some(tool => tool?.type !== "function")))
    throw new Error("Only local analysis functions are available.");
  messages(body.input);
}

export async function limitedBytes(request, max) {
  const length = Number(request.headers.get("content-length"));
  if (length > max) throw new Error("Request exceeds the size limit.");
  if (!request.body) return new Uint8Array();
  const reader = request.body.getReader();
  const chunks = [];
  let total = 0;
  while (true) {
    const {done, value} = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > max) { await reader.cancel(); throw new Error("Request exceeds the size limit."); }
    chunks.push(value);
  }
  const result = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) { result.set(chunk, offset); offset += chunk.byteLength; }
  return result;
}

export function cleanHeaders(headers) {
  const cleaned = new Headers(headers);
  for (const name of ["authorization", "cookie", "proxy-authorization", "x-api-key", "host",
    "cf-access-jwt-assertion", "x-analyst-role", "x-analyst-visitor", "x-analyst-internal"]) cleaned.delete(name);
  return cleaned;
}
