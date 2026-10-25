// A remote Shoal integration in plain Node.js (no dependencies).
//
// It speaks the shoal.integration/v1 protocol, so Shoal treats it like any built-in:
// its actions become workflow steps and agent tools, its resources feed campaign
// targets, and its webhooks fire triggers. The "incidents" backend is in memory
// to keep the example self-contained; a real service would call its provider API
// with the credentials it receives in `connection`.
//
//   SHOAL_INTEGRATION_TOKEN=change-me PORT=8080 node server.mjs

import { createHmac, timingSafeEqual } from "node:crypto";
import { createServer } from "node:http";

const PROTOCOL = "shoal.integration/v1";
const TOKEN = process.env.SHOAL_INTEGRATION_TOKEN;
if (!TOKEN && process.env.SHOAL_INSECURE_NO_AUTH !== "1") {
  console.error("set SHOAL_INTEGRATION_TOKEN (or SHOAL_INSECURE_NO_AUTH=1 for local development)");
  process.exit(1);
}

const manifest = {
  protocol: PROTOCOL,
  key: "incidents",
  title: "Incidents (Node example)",
  description: "Open and resolve incidents; incident webhooks fire triggers.",
  version: "1.0.0",
  connection_schema: {
    type: "object",
    properties: {
      api_key: { type: "string", description: "Provider API key." },
      webhook_secret: { type: "string", description: "HMAC secret for webhooks." },
    },
    required: ["api_key"],
  },
  secret_fields: ["api_key", "webhook_secret"],
  actions: [
    {
      name: "create_incident",
      description: "Open an incident.",
      input_schema: {
        type: "object",
        properties: { title: { type: "string" }, severity: { type: "string", enum: ["sev1", "sev2", "sev3"] } },
        required: ["title"],
      },
      output_schema: { type: "object", properties: { id: { type: "string" }, status: { type: "string" } } },
    },
    {
      name: "resolve_incident",
      description: "Resolve an incident.",
      input_schema: { type: "object", properties: { id: { type: "string" } }, required: ["id"] },
    },
  ],
  resources: [
    {
      name: "incident",
      description: "Incidents, filtered by status.",
      filter_schema: { type: "object", properties: { status: { type: "string" } } },
      key_field: "id",
    },
  ],
  events: [
    { name: "incident.opened", description: "An incident was opened.", example: { incident: { id: "inc-1" } } },
    { name: "incident.resolved", description: "An incident was resolved.", example: { incident: { id: "inc-1" } } },
  ],
  webhooks: true,
  test: true,
};

const incidents = new Map();

class HttpError extends Error {
  constructor(status, message, retryable = false) {
    super(message);
    this.status = status;
    this.retryable = retryable;
  }
}

function requireKey(connection) {
  if (!connection || typeof connection.api_key !== "string" || !connection.api_key) {
    throw new HttpError(422, "connection.api_key is required");
  }
}

function safeEqual(a, b) {
  const x = Buffer.from(String(a));
  const y = Buffer.from(String(b));
  return x.length === y.length && timingSafeEqual(x, y);
}

const routes = {
  "GET /manifest": () => manifest,
  "POST /test": ({ connection }) => {
    requireKey(connection);
    return { ok: true };
  },
  "POST /actions/create_incident": ({ connection, params }) => {
    requireKey(connection);
    if (!params?.title) throw new HttpError(422, "title is required");
    const id = `inc-${incidents.size + 1}`;
    const incident = { id, title: params.title, severity: params.severity ?? "sev3", status: "open" };
    incidents.set(id, incident);
    return { output: { id, status: incident.status } };
  },
  "POST /actions/resolve_incident": ({ connection, params }) => {
    requireKey(connection);
    const incident = incidents.get(params?.id);
    if (!incident) throw new HttpError(404, `no incident ${params?.id}`);
    incident.status = "resolved";
    return { output: { id: incident.id, status: incident.status } };
  },
  "POST /resources/incident/query": ({ connection, filter = {}, cursor, limit = 100 }) => {
    requireKey(connection);
    const all = [...incidents.values()].filter((i) => !filter.status || i.status === filter.status);
    const start = cursor ? Number(cursor) : 0;
    const items = all.slice(start, start + limit);
    return { items, next_cursor: start + limit < all.length ? String(start + limit) : null };
  },
  "POST /webhooks/verify": ({ connection, headers = {}, body_b64 = "" }) => {
    const secret = connection?.webhook_secret;
    const signature = headers["x-incident-signature"];
    if (!secret || !signature) return { valid: false };
    const body = Buffer.from(body_b64, "base64");
    const expected = "sha256=" + createHmac("sha256", secret).update(body).digest("hex");
    return { valid: safeEqual(signature, expected) };
  },
  "POST /webhooks/parse": ({ headers = {}, body_b64 = "" }) => {
    let payload;
    try {
      payload = JSON.parse(Buffer.from(body_b64, "base64").toString("utf8") || "{}");
    } catch {
      throw new HttpError(400, "body is not JSON");
    }
    const id = payload.incident?.id;
    return {
      events: [
        {
          type: `incident.${payload.action ?? "opened"}`,
          id: headers["x-delivery-id"] ?? null,
          data: payload,
          wake_keys: id ? [`incidents:${id}`] : [],
          actor: payload.user?.email ? { email: payload.user.email } : {},
        },
      ],
    };
  },
};

function send(res, status, body) {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(body));
}

export const server = createServer(async (req, res) => {
  const path = new URL(req.url, "http://localhost").pathname;
  if (path === "/healthz") return send(res, 200, { ok: true });
  if (TOKEN && !safeEqual(req.headers.authorization ?? "", `Bearer ${TOKEN}`)) {
    return send(res, 401, { error: { message: "missing or invalid bearer token", retryable: false } });
  }
  const handler = routes[`${req.method} ${path}`];
  if (!handler) return send(res, 404, { error: { message: `no route ${req.method} ${path}`, retryable: false } });
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  try {
    const raw = Buffer.concat(chunks).toString("utf8");
    const result = await handler(raw ? JSON.parse(raw) : {});
    send(res, 200, result);
  } catch (err) {
    const status = err instanceof HttpError ? err.status : err instanceof SyntaxError ? 400 : 500;
    send(res, status, { error: { message: err.message, retryable: status >= 500 || !!err.retryable } });
  }
});

const port = Number(process.env.PORT ?? 8080);
server.listen(port, process.env.HOST ?? "127.0.0.1", () => {
  console.log(`incidents integration listening on ${server.address().port}`);
});
