/**
 * The chat assistant, docked on the right of every page. It sets things up
 * through the public API with the user's own permissions; every change waits
 * on an approval card unless auto-approve is on.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ArrowLeftRight, ArrowUp, Bot, Check, ChevronRight, CircleAlert, Eye, History, MessageSquarePlus, MoreHorizontal, ShieldCheck, Sparkles, Trash2, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation } from "react-router";
import { ApiError, rawJson } from "@/api/client";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/cn";
import { useNs } from "@/lib/ns";
import { Badge, Button, CodeEditor, JsonBlock, Menu, MenuContent, MenuItem, MenuLabel, MenuSeparator, MenuTrigger, Spinner, Switch, Tooltip } from "@/components/ui";
import { Markdown } from "@/pages/docs/Markdown";
import {
  blocks,
  describeCall,
  resultStatus,
  resultText,
  resultsById,
  sendChat,
  type AssistantInfo,
  type Block,
  type ChatIn,
  type ChatMessage,
  type PendingAction,
} from "./api";
import { chatTitle, isNamespaceNote, NAMESPACE_NOTE, newChatId, useAssistant, useConversation, type Conversation } from "./store";

const NO_MESSAGES: ChatMessage[] = [];
/** The server accepts at most this many history messages (ChatIn.messages). */
const MAX_MESSAGES = 400;
const LONG_CHAT = 300;
const NO_PENDING: PendingAction[] = [];

const SUGGESTIONS = [
  "Create a workflow that fetches a Jira issue, summarises it with an agent and comments back",
  "Write a Python script step that dedupes and sorts a list of emails",
  "Run the hello workflow every weekday at 9am",
  "Why did my last run fail?",
];

export function useAssistantInfo(enabled = true) {
  return useQuery({
    queryKey: ["assistant-info"],
    queryFn: () => rawJson<AssistantInfo>("/v1/assistant"),
    enabled,
    staleTime: 5 * 60_000,
    retry: false,
  });
}

/** Header button that opens the panel (⌘J). */
export function AssistantLauncher() {
  const { open, setOpen } = useAssistant();
  const info = useAssistantInfo();
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "j") {
        e.preventDefault();
        useAssistant.getState().setOpen(!useAssistant.getState().open);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  if (info.data && !info.data.enabled) return null;
  return (
    <Tooltip content="Assistant (⌘J)">
      <Button variant={open ? "secondary" : "ghost"} size="sm" onClick={() => setOpen(!open)} icon={<Sparkles className="size-4" />} aria-pressed={open}>
        Assistant
      </Button>
    </Tooltip>
  );
}

export function AssistantPanel() {
  const { open, setOpen, draft, setDraft, autoApprove, setAutoApprove, load, save, select } = useAssistant();
  const conversation = useConversation();
  const { me } = useAuth();
  const ns = useNs();
  const location = useLocation();
  const qc = useQueryClient();
  const info = useAssistantInfo(open);
  const [error, setError] = useState<string | null>(null);
  const [showHistory, setShowHistory] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // One saved chat per person and tenant, so people sharing a browser don't see each other's history.
  useEffect(() => load(`${me?.tenant?.slug ?? "system"}:${me?.actor ?? "anonymous"}`), [load, me?.tenant?.slug, me?.actor]);
  useEffect(() => {
    if (open && draft) inputRef.current?.focus();
  }, [open, draft]);

  const messages = conversation?.messages ?? NO_MESSAGES;
  const pending = conversation?.pending ?? NO_PENDING;
  // The assistant follows the namespace the user is in; a switch since the chat's last message is sent along so it knows.
  const namespace = ns;
  const switchedFrom = conversation && messages.length > 0 && conversation.namespace !== ns ? conversation.namespace : null;
  const tenantName = me?.tenant?.name ?? me?.tenant?.slug ?? null;
  const results = useMemo(() => resultsById(messages), [messages]);

  const chat = useMutation({
    // The history travels with each call: the optimistic save below re-renders before the request is sent.
    mutationFn: (body: {
      id: string;
      message?: string;
      decisions?: Record<string, { approve: boolean; note?: string }>;
      history: ChatMessage[];
      pending: PendingAction[];
      previous: string | null;
    }) =>
      sendChat({
        namespace,
        previous_namespace: body.previous,
        messages: body.history as unknown as ChatIn["messages"],
        message: body.message ?? null,
        decisions: body.decisions ?? {},
        auto_approve: autoApprove,
        page: location.pathname,
        context: useAssistant.getState().pageContext,
      }),
    onMutate: (body) => {
      setError(null);
      // Show the user's message straight away; the response replaces the history.
      if (body.message) {
        const content = body.previous ? [{ type: "text", text: `${NAMESPACE_NOTE} to \`${namespace}\`` }, { type: "text", text: body.message }] : body.message;
        save({ id: body.id, messages: [...body.history, { role: "user", content }], pending: NO_PENDING, namespace, updatedAt: new Date().toISOString() });
      }
    },
    onSuccess: (out, body) => {
      save({ id: body.id, messages: out.messages as unknown as ChatMessage[], pending: out.pending ?? NO_PENDING, namespace, updatedAt: new Date().toISOString() });
      if (body.decisions && Object.values(body.decisions).some((d) => d.approve)) void qc.invalidateQueries();
      else if (autoApprove) void qc.invalidateQueries();
      if (out.stopped === "max_turns") setError("The assistant paused after many steps. Send “continue” to let it carry on.");
    },
    onError: (e, body) => {
      if (body.message) {
        if (body.history.length) save({ id: body.id, messages: body.history, pending: body.pending, namespace: body.previous ?? namespace, updatedAt: new Date().toISOString() });
        else useAssistant.getState().remove(body.id);
      }
      setError(
        e instanceof ApiError && e.status === 422 && body.history.length >= MAX_MESSAGES - 10
          ? "This chat has reached its length limit. Start a new chat to carry on."
          : e instanceof ApiError
            ? e.detail
            : String(e),
      );
      if (body.message) setDraft(body.message);
    },
  });

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages.length, pending.length, chat.isPending]);

  const send = (text: string) => {
    const message = text.trim();
    if (!message || chat.isPending) return;
    setDraft("");
    // A new message while actions wait declines them.
    chat.mutate({ id: conversation?.id ?? newChatId(), message, history: messages, pending, previous: switchedFrom });
  };

  const decide = (decisions: Record<string, { approve: boolean; note?: string }>) =>
    conversation && chat.mutate({ id: conversation.id, decisions, history: messages, pending, previous: switchedFrom });

  const reset = () => {
    select(null);
    setError(null);
    setShowHistory(false);
  };

  const openChat = (id: string) => {
    select(id);
    setError(null);
    setShowHistory(false);
  };

  if (!open) return null;
  const disabled = info.data && !info.data.enabled;

  return (
    <aside className="flex w-[420px] shrink-0 flex-col border-l border-border bg-panel" aria-label="Assistant">
      <div className="flex h-12 shrink-0 items-center gap-2 border-b border-border px-3">
        <div className="flex size-7 items-center justify-center rounded-lg bg-accent-soft text-accent">
          <Sparkles className="size-4" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-[13px] leading-tight font-semibold">Assistant</div>
          <div className="truncate text-[11px] text-faint">
            {tenantName ? `${tenantName} · ` : ""}
            {namespace}
            {info.data ? ` · ${info.data.model}` : ""}
            {autoApprove ? " · auto-approve" : ""}
          </div>
        </div>
        <Tooltip content="Earlier chats">
          <Button variant={showHistory ? "secondary" : "ghost"} size="icon-sm" aria-label="Earlier chats" aria-pressed={showHistory} onClick={() => setShowHistory(!showHistory)} disabled={chat.isPending}>
            <History className="size-4" />
          </Button>
        </Tooltip>
        <Tooltip content="New chat">
          <Button variant="ghost" size="icon-sm" aria-label="New chat" onClick={reset} disabled={chat.isPending}>
            <MessageSquarePlus className="size-4" />
          </Button>
        </Tooltip>
        <Menu>
          <MenuTrigger asChild>
            <Button variant="ghost" size="icon-sm" aria-label="Assistant settings">
              <MoreHorizontal className="size-4" />
            </Button>
          </MenuTrigger>
          <MenuContent className="w-72">
            <MenuLabel>Changes</MenuLabel>
            <div className="px-2 pb-2">
              <Switch checked={autoApprove} onCheckedChange={setAutoApprove} label={<span className="text-xs">Make changes without asking</span>} />
              <p className="mt-1 text-[11px] leading-snug text-faint">Off: every change waits for your approval. Your permissions apply either way.</p>
            </div>
            <MenuSeparator />
            <MenuItem icon={<MessageSquarePlus />} onSelect={reset}>
              New chat
            </MenuItem>
          </MenuContent>
        </Menu>
        <Button variant="ghost" size="icon-sm" aria-label="Close assistant" onClick={() => setOpen(false)}>
          <X className="size-4" />
        </Button>
      </div>

      <div ref={scrollRef} className="min-h-0 flex-1 space-y-3 overflow-y-auto px-3 py-3">
        {disabled ? (
          <Notice>The assistant is turned off on this server (SHOAL_ASSISTANT_ENABLED).</Notice>
        ) : showHistory ? (
          <ChatHistory currentId={conversation?.id ?? null} onOpen={openChat} onNew={reset} onBack={() => setShowHistory(false)} />
        ) : !messages.length ? (
          <Welcome onPick={(s) => send(s)} />
        ) : (
          <Transcript messages={messages} results={results} pending={pending} />
        )}
        {!showHistory && pending.length > 0 && !chat.isPending && <PendingCards pending={pending} tenant={tenantName} onDecide={decide} />}
        {chat.isPending && (
          <div className="flex items-center gap-2 px-1 text-xs text-muted">
            <Spinner className="size-3.5" /> Working…
          </div>
        )}
        {error && <Notice tone="error">{error}</Notice>}
      </div>

      {messages.length >= LONG_CHAT && (
        <div className="flex shrink-0 items-center gap-2 border-t border-border bg-subtle px-3 py-2 text-[12px] text-muted">
          <span className="min-w-0 flex-1">This chat is getting long, so replies are slower and it will stop at its length limit.</span>
          <Button size="sm" variant="ghost" onClick={reset} disabled={chat.isPending}>
            New chat
          </Button>
        </div>
      )}
      {!showHistory && switchedFrom && (
        <div className="flex shrink-0 items-center gap-2 border-t border-border bg-subtle px-3 py-2 text-[12px] text-muted">
          <span className="min-w-0 flex-1">
            You switched from <span className="font-mono text-fg">{switchedFrom}</span> to <span className="font-mono text-fg">{ns}</span>. The assistant carries on
            in <span className="font-mono text-fg">{ns}</span>{pending.length ? "; changes waiting for approval stay where their card says" : ""}.
          </span>
          <Button size="sm" variant="ghost" onClick={reset} disabled={chat.isPending}>
            New chat
          </Button>
        </div>
      )}

      <form
        className="shrink-0 border-t border-border p-3"
        onSubmit={(e) => {
          e.preventDefault();
          send(draft);
        }}
      >
        <div className="flex items-end gap-2 rounded-xl border border-border bg-subtle/40 p-1.5 focus-within:border-ring">
          <textarea
            ref={inputRef}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                send(draft);
              }
            }}
            rows={Math.min(6, Math.max(1, draft.split("\n").length))}
            placeholder={pending.length ? "Reply instead (declines the waiting changes)…" : "Ask or describe what to set up…"}
            className="max-h-40 min-h-8 flex-1 resize-none bg-transparent px-1.5 py-1 text-[13px] outline-none placeholder:text-faint"
            disabled={!!disabled}
          />
          <Button type="submit" size="icon-sm" aria-label="Send" disabled={!draft.trim() || chat.isPending || !!disabled}>
            <ArrowUp className="size-4" />
          </Button>
        </div>
        <p className="mt-1.5 px-1 text-[10.5px] text-faint">
          Acts with your permissions in {tenantName ? `${tenantName} · ` : ""}
          {namespace}. Never paste secret values here.
        </p>
      </form>
    </aside>
  );
}

function Welcome({ onPick }: { onPick: (s: string) => void }) {
  return (
    <div className="px-1 pt-4">
      <div className="mb-1 flex items-center gap-2 text-[15px] font-semibold">
        <Bot className="size-5 text-accent" /> What should we set up?
      </div>
      <p className="mb-4 text-[13px] leading-relaxed text-muted">
        I can build workflows, triggers, campaigns, evals and connections, write script steps so deterministic work runs as code instead of an agent, and explain
        what happened in a run. You approve every change.
      </p>
      <div className="space-y-1.5">
        {SUGGESTIONS.map((s) => (
          <button key={s} onClick={() => onPick(s)} className="flex w-full items-start gap-2 rounded-lg border border-border px-2.5 py-2 text-left text-[12.5px] text-muted transition-colors hover:border-border-strong hover:text-fg">
            <ChevronRight className="mt-0.5 size-3.5 shrink-0 text-faint" />
            {s}
          </button>
        ))}
      </div>
    </div>
  );
}

function Transcript({ messages, results, pending }: { messages: ChatMessage[]; results: Map<string, Block>; pending: PendingAction[] }) {
  const waiting = new Set(pending.map((p) => p.id));
  return (
    <>
      {messages.map((m, i) => {
        const bs = blocks(m);
        if (m.role === "user") {
          const switched = bs.find((b) => b.type === "text" && isNamespaceNote(b.text));
          const text = bs.filter((b) => b.type === "text" && !isNamespaceNote(b.text)).map((b) => b.text).join("\n");
          if (!text && !switched) return null;
          return (
            <div key={i} className="space-y-3">
              {switched && <NamespaceDivider note={switched.text ?? ""} />}
              {text && <div className="ml-8 rounded-xl rounded-tr-sm bg-accent-soft px-3 py-2 text-[13px] whitespace-pre-wrap text-fg">{text}</div>}
            </div>
          );
        }
        return (
          <div key={i} className="space-y-1.5">
            {bs.map((b, j) => {
              if (b.type === "text" && b.text?.trim())
                return <Markdown key={j} source={b.text} slug="" className="prose-shoal prose-chat" />;
              if (b.type === "tool_use" && b.id) return <ToolRow key={j} call={b} result={results.get(b.id)} waiting={waiting.has(b.id)} />;
              return null;
            })}
          </div>
        );
      })}
    </>
  );
}

/** "Switched to namespace ops" between turns, from the note the server adds to the user's turn. */
function NamespaceDivider({ note }: { note: string }) {
  const to = note.match(/to `([^`]+)`/)?.[1];
  return (
    <div className="flex items-center gap-2 text-[11px] text-faint" role="separator">
      <div className="h-px flex-1 bg-border" />
      <ArrowLeftRight className="size-3" />
      <span>
        Switched to namespace <span className="font-mono text-muted">{to ?? "?"}</span>
      </span>
      <div className="h-px flex-1 bg-border" />
    </div>
  );
}

/** Where a change goes: the namespace it names in its input or path, if any. */
function callNamespace(call: Block): string | null {
  const i = call.input ?? {};
  if (typeof i.namespace === "string" && i.namespace) return i.namespace;
  const m = typeof i.path === "string" ? i.path.match(/^\/v1\/namespaces\/([^/?]+)/) : null;
  if (m) return decodeURIComponent(m[1]);
  const q = i.query as Record<string, unknown> | undefined;
  return typeof q?.namespace === "string" ? q.namespace : null;
}

function ToolRow({ call, result, waiting }: { call: Block; result?: Block; waiting: boolean }) {
  const [open, setOpen] = useState(false);
  const change = call.name === "apply_resources" || call.name === "api_call";
  const status = resultStatus(result);
  const declined = result?.is_error && resultText(result).startsWith("The user declined");
  const failed = result?.is_error && !declined;
  const tone = waiting ? "warn" : declined ? "neutral" : failed ? "bad" : change ? "ok" : "neutral";
  const label = waiting ? "Waiting for you" : !result ? "" : declined ? "Declined" : failed ? (status ? `Failed (${status})` : "Failed") : change ? "Done" : "";
  return (
    <div className={cn("rounded-lg border text-[12px]", change ? "border-border bg-subtle/40" : "border-transparent")}>
      <button onClick={() => setOpen(!open)} className="flex w-full items-center gap-1.5 px-2 py-1 text-left text-muted hover:text-fg">
        <ChevronRight className={cn("size-3 shrink-0 transition-transform", open && "rotate-90")} />
        {change ? <ShieldCheck className="size-3.5 shrink-0" /> : <Eye className="size-3.5 shrink-0" />}
        <span className="min-w-0 flex-1 truncate">{describeCall(call)}</span>
        {change && callNamespace(call) && <span className="shrink-0 font-mono text-[11px] text-faint">{callNamespace(call)}</span>}
        {label && (
          <Badge tone={tone} className="shrink-0">
            {label}
          </Badge>
        )}
      </button>
      {open && (
        <div className="space-y-1.5 px-2 pb-2">
          {call.name === "apply_resources" && typeof call.input?.yaml === "string" ? (
            <CodeEditor value={call.input.yaml} readOnly language="yaml" maxHeight="260px" className="rounded-md border border-border text-[11.5px]" lineNumbers={false} />
          ) : (
            <JsonBlock value={call.input} maxHeight="200px" />
          )}
          {result && <JsonBlock value={parse(resultText(result))} maxHeight="240px" />}
        </div>
      )}
    </div>
  );
}

function parse(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function PendingCards({ pending, tenant, onDecide }: { pending: PendingAction[]; tenant: string | null; onDecide: (d: Record<string, { approve: boolean; note?: string }>) => void }) {
  const [decided, setDecided] = useState<Record<string, boolean>>({});
  const decideOne = (id: string, approve: boolean) => {
    const next = { ...decided, [id]: approve };
    setDecided(next);
    if (pending.every((p) => p.id in next)) onDecide(Object.fromEntries(Object.entries(next).map(([k, v]) => [k, { approve: v }])));
  };
  return (
    <div className="space-y-2">
      {pending.map((p) => (
        <ActionCard key={p.id} action={p} tenant={tenant} decided={decided[p.id]} onDecide={(approve) => decideOne(p.id, approve)} />
      ))}
      {pending.length > 1 && Object.keys(decided).length === 0 && (
        <div className="flex justify-end gap-2">
          <Button size="sm" variant="ghost" onClick={() => onDecide(Object.fromEntries(pending.map((p) => [p.id, { approve: false }])))}>
            Decline all
          </Button>
          <Button size="sm" icon={<Check className="size-3.5" />} onClick={() => onDecide(Object.fromEntries(pending.map((p) => [p.id, { approve: true }])))}>
            Approve all
          </Button>
        </div>
      )}
    </div>
  );
}

const OUTCOME_TONE: Record<string, "ok" | "info" | "neutral"> = { created: "ok", updated: "info", unchanged: "neutral" };

function ActionCard({ action, tenant, decided, onDecide }: { action: PendingAction; tenant: string | null; decided?: boolean; onDecide: (approve: boolean) => void }) {
  const [show, setShow] = useState(false);
  const plan = (Array.isArray(action.plan) ? action.plan : []) as { kind: string; name: string; namespace: string; outcome: string }[];
  const destructive = action.method === "DELETE";
  return (
    <div className={cn("rounded-xl border bg-panel p-3 shadow-card", destructive ? "border-bad/40" : "border-warn/40")}>
      <div className="mb-1 flex items-center gap-1.5 text-[11px] font-semibold tracking-wide text-warn uppercase">
        <ShieldCheck className="size-3.5" /> Approve this change?
      </div>
      <div className="mb-1 text-[13px] font-medium">{action.summary}</div>
      <div className="mb-2 text-[11.5px] text-muted">
        {action.namespace ? (
          <>
            In namespace <span className="font-mono text-fg">{action.namespace}</span>
            {tenant ? <> of {tenant}</> : null}
          </>
        ) : (
          <>Applies to the whole {tenant ? <>{tenant} </> : null}tenant</>
        )}
      </div>
      {plan.length > 0 ? (
        <ul className="mb-2 space-y-1">
          {plan.map((d) => (
            <li key={`${d.kind}/${d.name}`} className="flex items-center gap-2 text-[12px]">
              <Badge tone={OUTCOME_TONE[d.outcome] ?? "neutral"}>{d.outcome}</Badge>
              <span className="text-muted">{d.kind}</span>
              <span className="font-mono">{d.name}</span>
              <span className="text-faint">in {d.namespace}</span>
            </li>
          ))}
        </ul>
      ) : (
        <div className="mb-2 font-mono text-[11.5px] break-all text-muted">
          <span className={cn("font-semibold", destructive ? "text-bad" : "text-fg")}>{action.method}</span> {action.path}
        </div>
      )}
      <button onClick={() => setShow(!show)} className="mb-2 flex items-center gap-1 text-[11.5px] text-muted hover:text-fg">
        <ChevronRight className={cn("size-3 transition-transform", show && "rotate-90")} /> {action.yaml ? "Show YAML" : "Show request"}
      </button>
      {show &&
        (action.yaml ? (
          <CodeEditor value={action.yaml} readOnly language="yaml" maxHeight="320px" className="mb-2 rounded-md border border-border text-[11.5px]" lineNumbers={false} />
        ) : (
          <div className="mb-2">
            <JsonBlock value={{ query: action.query, body: action.body }} maxHeight="240px" />
          </div>
        ))}
      {decided === undefined ? (
        <div className="flex justify-end gap-2">
          <Button size="sm" variant="ghost" onClick={() => onDecide(false)}>
            Decline
          </Button>
          <Button size="sm" variant={destructive ? "danger" : "primary"} icon={<Check className="size-3.5" />} onClick={() => onDecide(true)}>
            {action.tool === "apply_resources" ? "Apply" : "Approve"}
          </Button>
        </div>
      ) : (
        <div className="text-right text-xs text-muted">{decided ? "Approved" : "Declined"}</div>
      )}
    </div>
  );
}

function ChatHistory({ currentId, onOpen, onNew, onBack }: { currentId: string | null; onOpen: (id: string) => void; onNew: () => void; onBack: () => void }) {
  const { chats, remove } = useAssistant();
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2 px-1 pb-1">
        <Button variant="ghost" size="icon-sm" aria-label="Back to chat" onClick={onBack}>
          <ArrowLeft className="size-4" />
        </Button>
        <div className="flex-1 text-[13px] font-semibold">Earlier chats</div>
        <Button size="sm" variant="ghost" icon={<MessageSquarePlus className="size-3.5" />} onClick={onNew}>
          New chat
        </Button>
      </div>
      {!chats.length && <p className="px-1 text-[12.5px] text-muted">No chats yet.</p>}
      {chats.map((c: Conversation) => (
        <div
          key={c.id}
          className={cn(
            "group flex items-start gap-2 rounded-lg border px-2.5 py-2 transition-colors hover:border-border-strong",
            c.id === currentId ? "border-accent/40 bg-accent-soft/40" : "border-border",
          )}
        >
          <button className="min-w-0 flex-1 text-left" onClick={() => onOpen(c.id)}>
            <div className="truncate text-[12.5px] text-fg">{chatTitle(c)}</div>
            <div className="truncate text-[11px] text-faint">
              <span className="font-mono">{c.namespace}</span> · {new Date(c.updatedAt).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}
              {c.pending.length ? " · waiting for approval" : ""}
            </div>
          </button>
          <Tooltip content="Delete chat">
            <Button variant="ghost" size="icon-sm" aria-label="Delete chat" className="opacity-0 group-hover:opacity-100 focus-visible:opacity-100" onClick={() => remove(c.id)}>
              <Trash2 className="size-3.5" />
            </Button>
          </Tooltip>
        </div>
      ))}
    </div>
  );
}

function Notice({ children, tone }: { children: React.ReactNode; tone?: "error" }) {
  return (
    <div className={cn("flex items-start gap-2 rounded-lg border px-2.5 py-2 text-[12.5px]", tone === "error" ? "border-bad/30 bg-bad-soft text-bad" : "border-border bg-subtle text-muted")}>
      <CircleAlert className="mt-0.5 size-3.5 shrink-0" />
      <span>{children}</span>
    </div>
  );
}
