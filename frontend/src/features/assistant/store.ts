/**
 * Assistant panel state. The server keeps no conversations: the history lives
 * here (and in localStorage, per person and tenant) and is sent with every
 * message. Earlier chats are kept so "New chat" never loses one.
 */
import { create } from "zustand";
import type { ChatMessage, PendingAction } from "./api";

export interface Conversation {
  id: string;
  messages: ChatMessage[];
  pending: PendingAction[];
  /** The namespace the chat last worked in; a switch since is sent with the next message. */
  namespace: string;
  updatedAt: string;
}

interface AssistantState {
  open: boolean;
  /** Text placed in the composer by "Ask the assistant" buttons elsewhere in the UI. */
  draft: string;
  autoApprove: boolean;
  scope: string;
  /** Saved chats, newest first. */
  chats: Conversation[];
  /** The open chat; null for a new one. */
  currentId: string | null;
  /** What the current page shows that the API can't (e.g. unsaved builder edits); sent with each message. */
  pageContext: string | null;
  setPageContext: (context: string | null) => void;
  setOpen: (open: boolean) => void;
  ask: (draft: string) => void;
  setDraft: (draft: string) => void;
  setAutoApprove: (v: boolean) => void;
  load: (scope: string) => void;
  /** Store a chat (by id) and make it the open one. */
  save: (c: Conversation) => void;
  /** Open a saved chat, or null to start a new one. */
  select: (id: string | null) => void;
  remove: (id: string) => void;
}

const KEY = "shoal.assistant";
const PREFS = "shoal.assistant.prefs";
/** Older chats beyond this are dropped. */
export const MAX_CHATS = 30;

function readJson<T>(key: string): T | null {
  try {
    const v = localStorage.getItem(key);
    return v ? (JSON.parse(v) as T) : null;
  } catch {
    return null;
  }
}

function writeJson(key: string, value: unknown): boolean {
  try {
    if (value == null) localStorage.removeItem(key);
    else localStorage.setItem(key, JSON.stringify(value));
    return true;
  } catch {
    return false; /* private mode or full: the chat still works for this tab */
  }
}

export function newChatId(): string {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

interface Saved {
  chats: Conversation[];
  currentId: string | null;
}

function readChats(scope: string): Saved {
  const saved = readJson<Saved>(`${KEY}.chats:${scope}`);
  if (saved?.chats) return saved;
  // Before chat history there was one chat per scope.
  const old = readJson<Omit<Conversation, "id">>(`${KEY}:${scope}`);
  if (old?.messages?.length) {
    const chat = { ...old, id: newChatId() };
    return { chats: [chat], currentId: chat.id };
  }
  return { chats: [], currentId: null };
}

/** Write the chats; when storage is full, drop the oldest ones (never the open one) until they fit. */
function writeChats(scope: string, saved: Saved): Conversation[] {
  let chats = saved.chats.slice(0, MAX_CHATS);
  while (!writeJson(`${KEY}.chats:${scope}`, { ...saved, chats })) {
    const drop = chats.findLastIndex((c) => c.id !== saved.currentId);
    if (drop < 0) break;
    chats = chats.filter((_, i) => i !== drop);
  }
  writeJson(`${KEY}:${scope}`, null);
  return chats;
}

const prefs = readJson<{ open?: boolean; autoApprove?: boolean }>(PREFS) ?? {};

export const useAssistant = create<AssistantState>((set, get) => {
  const persist = (chats: Conversation[], currentId: string | null) => set({ chats: writeChats(get().scope, { chats, currentId }), currentId });
  return {
    open: prefs.open ?? false,
    draft: "",
    autoApprove: prefs.autoApprove ?? false,
    scope: "",
    chats: [],
    currentId: null,
    pageContext: null,
    setPageContext: (pageContext) => set({ pageContext }),
    setOpen: (open) => {
      set({ open });
      writeJson(PREFS, { open, autoApprove: get().autoApprove });
    },
    ask: (draft) => {
      set({ draft });
      get().setOpen(true);
    },
    setDraft: (draft) => set({ draft }),
    setAutoApprove: (autoApprove) => {
      set({ autoApprove });
      writeJson(PREFS, { open: get().open, autoApprove });
    },
    load: (scope) => {
      if (scope === get().scope) return;
      const saved = readChats(scope);
      set({ scope, currentId: saved.currentId, chats: writeChats(scope, saved) });
    },
    save: (conversation) => persist([conversation, ...get().chats.filter((c) => c.id !== conversation.id)], conversation.id),
    select: (currentId) => persist(get().chats, currentId),
    remove: (id) => {
      const { chats, currentId } = get();
      persist(
        chats.filter((c) => c.id !== id),
        currentId === id ? null : currentId,
      );
    },
  };
});

/** The open chat, if any. */
export function useConversation(): Conversation | null {
  return useAssistant((s) => s.chats.find((c) => c.id === s.currentId) ?? null);
}

/** A chat's title: its first message. */
export function chatTitle(c: Conversation): string {
  for (const m of c.messages) {
    if (m.role !== "user") continue;
    const text = typeof m.content === "string" ? m.content : m.content.find((b) => b.type === "text" && !isNamespaceNote(b.text))?.text;
    if (text?.trim()) return text.trim().split("\n")[0].slice(0, 120);
  }
  return "New chat";
}

export const NAMESPACE_NOTE = "[Shoal: namespace changed]";

export function isNamespaceNote(text: string | undefined): boolean {
  return !!text?.startsWith(NAMESPACE_NOTE);
}
