/**
 * Keeps one failing panel from taking the whole page down. A render error in
 * the children shows a small notice in their place; the rest of the page keeps
 * working, and the panel tries again when `resetKeys` change (the document it
 * renders was edited, the route changed) or when "Try again" is pressed.
 */
import { CircleAlert, RotateCcw } from "lucide-react";
import { Component, type ErrorInfo, type ReactNode } from "react";
import { cn } from "@/lib/cn";
import { isChunkLoadError } from "@/lib/stale";

interface Props {
  children: ReactNode;
  /** What failed, for the notice: "the canvas", "the YAML view". */
  label?: string;
  /** Any change to these clears the error and renders the children again. */
  resetKeys?: unknown[];
  className?: string;
  /** Replaces the default notice. */
  fallback?: (error: Error, reset: () => void) => ReactNode;
}

interface State {
  error: Error | null;
  keys: unknown[] | undefined;
}

const changed = (a: unknown[] = [], b: unknown[] = []) => a.length !== b.length || a.some((v, i) => !Object.is(v, b[i]));

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, keys: this.props.resetKeys };

  static getDerivedStateFromError(error: unknown): Partial<State> {
    return { error: error instanceof Error ? error : new Error(String(error)) };
  }

  static getDerivedStateFromProps(props: Props, state: State): Partial<State> | null {
    if (!changed(props.resetKeys, state.keys)) return null;
    return { keys: props.resetKeys, error: null };
  }

  componentDidCatch(error: unknown, info: ErrorInfo) {
    console.error(`Error in ${this.props.label ?? "a panel"}:`, error, info.componentStack);
  }

  reset = () => this.setState({ error: null });

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    // A page chunk from before a redeploy: let the route's error page reload into the new build.
    if (isChunkLoadError(error)) throw error;
    if (this.props.fallback) return this.props.fallback(error, this.reset);
    return <PanelError error={error} label={this.props.label} onRetry={this.reset} className={this.props.className} />;
  }
}

export function PanelError({ error, label, onRetry, className }: { error: Error; label?: string; onRetry?: () => void; className?: string }) {
  return (
    <div role="alert" className={cn("flex h-full min-h-24 flex-col items-center justify-center gap-2 p-6 text-center", className)}>
      <CircleAlert className="size-5 text-bad" />
      <div className="text-[13px] font-medium">{label ? `Could not show ${label}` : "Something went wrong here"}</div>
      <div className="max-w-md font-mono text-[11.5px] break-words text-muted">{error.message}</div>
      {onRetry && (
        <button type="button" onClick={onRetry} className="mt-1 inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-muted hover:bg-subtle hover:text-fg">
          <RotateCcw className="size-3.5" /> Try again
        </button>
      )}
    </div>
  );
}
