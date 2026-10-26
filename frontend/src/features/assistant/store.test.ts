import { beforeAll, describe, expect, it } from "vitest";
import type { Conversation } from "./store";

const data = new Map<string, string>();
let store: typeof import("./store");

beforeAll(async () => {
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: (k: string) => data.get(k) ?? null,
      setItem: (k: string, v: string) => void data.set(k, v),
      removeItem: (k: string) => void data.delete(k),
    },
  });
  store = await import("./store");
});

const chat = (id: string, text: string): Conversation => ({
  id,
  messages: [{ role: "user", content: text }],
  pending: [],
  namespace: "default",
  updatedAt: "2026-10-26T00:00:00Z",
});

describe("assistant chat history", () => {
  it("keeps earlier chats when starting a new one, and reopens them", () => {
    data.set("shoal.assistant:acme:user:ana", JSON.stringify({ messages: [{ role: "user", content: "old chat" }], pending: [], namespace: "ops", updatedAt: "x" }));
    const s = store.useAssistant.getState();
    s.load("acme:user:ana");
    // the single chat saved before history existed is carried over
    expect(store.useAssistant.getState().chats.map(store.chatTitle)).toEqual(["old chat"]);
    expect(data.has("shoal.assistant:acme:user:ana")).toBe(false);
    const oldId = store.useAssistant.getState().currentId!;

    s.select(null);
    s.save(chat("b", "second"));
    expect(store.useAssistant.getState().chats.map((c) => c.id)).toEqual(["b", oldId]);
    s.select(oldId);
    expect(store.useAssistant.getState().currentId).toBe(oldId);
    s.remove(oldId);
    expect(store.useAssistant.getState()).toMatchObject({ currentId: null, chats: [{ id: "b" }] });
  });

  it("titles a chat by its first message, skipping namespace notes", () => {
    const c = chat("c", "");
    c.messages = [{ role: "user", content: [{ type: "text", text: `${store.NAMESPACE_NOTE} to \`ops\`` }, { type: "text", text: "run triage\nplease" }] }];
    expect(store.chatTitle(c)).toBe("run triage");
  });
});
