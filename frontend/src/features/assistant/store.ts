/**
 * Assistant panel state. The server keeps no conversations: the history lives
 * here (and in localStorage, per tenant) and is sent with every message.
 */
import { create } from "zustand";
import type { ChatMessage, PendingAction } from "./api";

export interface Conversation {
  messages: ChatMessage[];
  pending: PendingAction[];
  namespace: string;
  updatedAt: string;
}

interface AssistantState {
  open: boolean;
  /** Text placed in the composer by "Ask the assistant" buttons elsewhere in the UI. */
  draft: string;
  autoApprove: boolean;
  scope: string;
  conversation: Conversation | null;
  setOpen: (open: boolean) => void;
  ask: (draft: string) => void;
  setDraft: (draft: string) => void;
  setAutoApprove: (v: boolean) => void;
  load: (scope: string) => void;
  save: (c: Conversation | null) => void;
}

const KEY = "shoal.assistant";
const PREFS = "shoal.assistant.prefs";

function readJson<T>(key: string): T | null {
  try {
    const v = localStorage.getItem(key);
    return v ? (JSON.parse(v) as T) : null;
  } catch {
    return null;
  }
}

function writeJson(key: string, value: unknown) {
  try {
    if (value == null) localStorage.removeItem(key);
    else localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* private mode or full: the chat still works for this tab */
  }
}

const prefs = readJson<{ open?: boolean; autoApprove?: boolean }>(PREFS) ?? {};

export const useAssistant = create<AssistantState>((set, get) => ({
  open: prefs.open ?? false,
  draft: "",
  autoApprove: prefs.autoApprove ?? false,
  scope: "",
  conversation: null,
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
    set({ scope, conversation: readJson<Conversation>(`${KEY}:${scope}`) });
  },
  save: (conversation) => {
    set({ conversation });
    writeJson(`${KEY}:${get().scope}`, conversation);
  },
}));
