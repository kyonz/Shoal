/** `POST /v1/assistant/chat` and helpers to read the Anthropic-shaped history it returns. */
import { rawJson, type Schemas } from "@/api/client";

export type PendingAction = Schemas["PendingAction"];
export type ChatOut = Schemas["ChatOut"];
export type ChatIn = Schemas["ChatIn"];
export type AssistantInfo = Schemas["AssistantInfo"];

export interface Block {
  type: string;
  text?: string;
  id?: string;
  name?: string;
  input?: Record<string, unknown>;
  tool_use_id?: string;
  content?: unknown;
  is_error?: boolean;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string | Block[];
}

export async function sendChat(body: ChatIn): Promise<ChatOut> {
  return rawJson<ChatOut>("/v1/assistant/chat", { method: "POST", body: JSON.stringify(body) });
}

export function blocks(m: ChatMessage): Block[] {
  return typeof m.content === "string" ? [{ type: "text", text: m.content }] : m.content;
}

/** tool_use id -> its tool_result block, across the whole history. */
export function resultsById(messages: ChatMessage[]): Map<string, Block> {
  const map = new Map<string, Block>();
  for (const m of messages) for (const b of blocks(m)) if (b.type === "tool_result" && b.tool_use_id) map.set(b.tool_use_id, b);
  return map;
}

/** The API status a tool result reports (`{"status": 201, ...}`), if any. */
export function resultStatus(result: Block | undefined): number | null {
  if (!result || typeof result.content !== "string") return null;
  const m = result.content.match(/^\{"status": (\d{3})/);
  return m ? Number(m[1]) : null;
}

export function resultText(result: Block | undefined): string {
  if (!result) return "";
  if (typeof result.content === "string") return result.content;
  return JSON.stringify(result.content, null, 2);
}

/** One line describing a tool call, for the activity list. */
export function describeCall(b: Block): string {
  const i = b.input ?? {};
  switch (b.name) {
    case "api_get":
      return `Read ${String(i.path ?? "")}`;
    case "list_endpoints":
      return `Looked up API endpoints${i.tag ? ` (${String(i.tag)})` : i.search ? ` matching “${String(i.search)}”` : ""}`;
    case "describe_endpoint":
      return `Read the docs for ${String(i.method ?? "").toUpperCase()} ${String(i.path ?? "")}`;
    case "apply_resources":
    case "api_call":
      return String(i.summary ?? b.name);
    default:
      return b.name ?? "tool";
  }
}
