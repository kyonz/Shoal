/**
 * Small, consistent building blocks. Everything uses the semantic colour
 * tokens from index.css so both themes stay in sync.
 */
import { CloudOff, Loader2, Lock } from "lucide-react";
import { FEDERATION_READ_ONLY, federationCode, federationMessage } from "@/lib/federation";
import {
  forwardRef,
  type ButtonHTMLAttributes,
  type HTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";
import { cn } from "@/lib/cn";

// --------------------------------------------------------------------------- button

type Variant = "primary" | "secondary" | "ghost" | "danger" | "subtle";
type Size = "xs" | "sm" | "md" | "icon" | "icon-sm";

const variants: Record<Variant, string> = {
  primary: "bg-accent text-accent-fg hover:brightness-110 shadow-card border border-transparent",
  secondary: "bg-panel text-fg border border-border hover:bg-subtle hover:border-border-strong shadow-card",
  ghost: "text-muted hover:text-fg hover:bg-subtle border border-transparent",
  subtle: "bg-subtle text-fg hover:bg-border/60 border border-transparent",
  danger: "bg-bad text-white hover:brightness-110 border border-transparent shadow-card",
};

const sizes: Record<Size, string> = {
  xs: "h-6 px-2 text-xs gap-1 rounded-md",
  sm: "h-7 px-2.5 text-[13px] gap-1.5 rounded-md",
  md: "h-8 px-3 text-[13px] gap-2 rounded-lg",
  icon: "h-8 w-8 justify-center rounded-lg",
  "icon-sm": "h-7 w-7 justify-center rounded-md",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
  icon?: ReactNode;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", size = "md", loading, icon, className, children, disabled, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      disabled={disabled || loading}
      className={cn(
        "inline-flex shrink-0 items-center font-medium whitespace-nowrap transition-[background,color,border,filter] duration-100 select-none",
        "disabled:pointer-events-none disabled:opacity-50",
        variants[variant],
        sizes[size],
        className,
      )}
      {...rest}
    >
      {loading ? <Loader2 className="size-3.5 animate-spin" /> : icon}
      {children}
    </button>
  );
});

// --------------------------------------------------------------------------- inputs

const field =
  "w-full rounded-lg border border-border bg-panel text-fg placeholder:text-faint shadow-card transition-colors " +
  "hover:border-border-strong focus:border-ring focus:outline-none focus:ring-2 focus:ring-ring/25 disabled:opacity-60";

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement> & { mono?: boolean }>(
  function Input({ className, mono, ...rest }, ref) {
    return <input ref={ref} className={cn(field, "h-8 px-2.5 text-[13px]", mono && "font-mono text-xs", className)} {...rest} />;
  },
);

export const Textarea = forwardRef<
  HTMLTextAreaElement,
  TextareaHTMLAttributes<HTMLTextAreaElement> & { mono?: boolean }
>(function Textarea({ className, mono, ...rest }, ref) {
  return (
    <textarea
      ref={ref}
      className={cn(field, "min-h-20 px-2.5 py-2 text-[13px] leading-relaxed", mono && "font-mono text-xs", className)}
      {...rest}
    />
  );
});

export const NativeSelect = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(
  function NativeSelect({ className, children, ...rest }, ref) {
    return (
      <select ref={ref} className={cn(field, "h-8 cursor-pointer px-2 pr-7 text-[13px]", className)} {...rest}>
        {children}
      </select>
    );
  },
);

export function Label({ children, hint, required, htmlFor, className }: {
  children: ReactNode;
  hint?: ReactNode;
  required?: boolean;
  htmlFor?: string;
  className?: string;
}) {
  return (
    <label htmlFor={htmlFor} className={cn("mb-1 flex items-baseline gap-2 text-xs font-medium text-fg", className)}>
      <span>
        {children}
        {required && <span className="ml-0.5 text-bad">*</span>}
      </span>
      {hint && <span className="truncate font-normal text-faint">{hint}</span>}
    </label>
  );
}

export function Field({ label, hint, required, error, children, className, description }: {
  label: ReactNode;
  hint?: ReactNode;
  description?: ReactNode;
  required?: boolean;
  error?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("min-w-0", className)}>
      <Label hint={hint} required={required}>
        {label}
      </Label>
      {children}
      {description && !error && <p className="mt-1 text-xs leading-snug text-faint">{description}</p>}
      {error && <p className="mt-1 text-xs text-bad">{error}</p>}
    </div>
  );
}

// --------------------------------------------------------------------------- surfaces

export function Card({ className, children, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={cn("rounded-xl border border-border bg-panel shadow-card", className)} {...rest}>
      {children}
    </div>
  );
}

export function CardHeader({ title, description, actions, className }: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex items-start justify-between gap-3 border-b border-border px-4 py-3", className)}>
      <div className="min-w-0">
        <h3 className="text-[13px] font-semibold">{title}</h3>
        {description && <p className="mt-0.5 text-xs text-muted">{description}</p>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-1.5">{actions}</div>}
    </div>
  );
}

export function PageHeader({ title, description, actions, breadcrumb, icon }: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  breadcrumb?: ReactNode;
  icon?: ReactNode;
}) {
  return (
    <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
      <div className="min-w-0">
        {breadcrumb && <div className="mb-1.5 flex items-center gap-1 text-xs text-muted">{breadcrumb}</div>}
        <div className="flex items-center gap-2.5">
          {icon && (
            <div className="flex size-8 items-center justify-center rounded-lg border border-border bg-panel text-accent shadow-card">
              {icon}
            </div>
          )}
          <h1 className="truncate text-xl font-semibold tracking-tight">{title}</h1>
        </div>
        {description && <p className="mt-1 max-w-3xl text-[13px] text-muted">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Page({ children, className, wide }: { children: ReactNode; className?: string; wide?: boolean }) {
  return <div className={cn("mx-auto w-full px-6 py-6", wide ? "max-w-[1600px]" : "max-w-6xl", className)}>{children}</div>;
}

// --------------------------------------------------------------------------- badges

type Tone = "neutral" | "accent" | "ok" | "warn" | "bad" | "info";

const tones: Record<Tone, string> = {
  neutral: "bg-idle-soft text-idle border-idle/15",
  accent: "bg-accent-soft text-accent border-accent/20",
  ok: "bg-ok-soft text-ok border-ok/20",
  warn: "bg-warn-soft text-warn border-warn/20",
  bad: "bg-bad-soft text-bad border-bad/20",
  info: "bg-info-soft text-info border-info/20",
};

export function Badge({ tone = "neutral", children, className, dot, title }: {
  tone?: Tone;
  children: ReactNode;
  className?: string;
  dot?: boolean;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex h-5 items-center gap-1 rounded-md border px-1.5 text-[11px] font-medium whitespace-nowrap",
        tones[tone],
        className,
      )}
    >
      {dot && <span className="size-1.5 rounded-full bg-current" />}
      {children}
    </span>
  );
}

export function Kbd({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <kbd
      className={cn(
        "inline-flex h-[18px] min-w-[18px] items-center justify-center rounded border border-border bg-subtle px-1 font-sans text-[10px] font-medium text-muted",
        className,
      )}
    >
      {children}
    </kbd>
  );
}

export function Spinner({ className }: { className?: string }) {
  return <Loader2 className={cn("size-4 animate-spin text-muted", className)} />;
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={cn("animate-pulse rounded-md bg-subtle", className)} />;
}

export function EmptyState({ icon, title, description, action, className }: {
  icon?: ReactNode;
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-col items-center justify-center px-6 py-14 text-center", className)}>
      {icon && (
        <div className="mb-3 flex size-11 items-center justify-center rounded-xl border border-border bg-subtle text-muted">
          {icon}
        </div>
      )}
      <h3 className="text-sm font-semibold">{title}</h3>
      {description && <p className="mt-1 max-w-sm text-[13px] text-muted">{description}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function ErrorNotice({ error, className }: { error: unknown; className?: string }) {
  if (!error) return null;
  const fed = federationCode(error);
  if (fed)
    return (
      <div className={cn("flex items-start gap-2 rounded-lg border border-warn/25 bg-warn-soft px-3 py-2 text-[13px] text-warn", className)}>
        {fed !== FEDERATION_READ_ONLY ? <CloudOff className="mt-0.5 size-3.5 shrink-0" /> : <Lock className="mt-0.5 size-3.5 shrink-0" />}
        <span>{federationMessage(fed)}</span>
      </div>
    );
  const msg = error instanceof Error ? error.message : String(error);
  return (
    <div className={cn("rounded-lg border border-bad/25 bg-bad-soft px-3 py-2 text-[13px] text-bad", className)}>{msg}</div>
  );
}

export function Divider({ className }: { className?: string }) {
  return <div className={cn("h-px bg-border", className)} />;
}

export function SectionTitle({ children, actions, className }: { children: ReactNode; actions?: ReactNode; className?: string }) {
  return (
    <div className={cn("mb-2 flex items-center justify-between", className)}>
      <h4 className="text-[11px] font-semibold tracking-wider text-faint uppercase">{children}</h4>
      {actions}
    </div>
  );
}

// --------------------------------------------------------------------------- table

export function Table({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={cn("overflow-x-auto", className)}>
      <table className="w-full border-collapse text-[13px]">{children}</table>
    </div>
  );
}

export function Th({ children, className }: { children?: ReactNode; className?: string }) {
  return (
    <th
      className={cn(
        "h-9 border-b border-border px-3 text-left text-[11px] font-semibold tracking-wide whitespace-nowrap text-faint uppercase first:pl-4 last:pr-4",
        className,
      )}
    >
      {children}
    </th>
  );
}

export function Td({ children, className, ...rest }: HTMLAttributes<HTMLTableCellElement>) {
  return (
    <td className={cn("h-11 border-b border-border px-3 align-middle first:pl-4 last:pr-4", className)} {...rest}>
      {children}
    </td>
  );
}

export function Tr({ children, className, onClick }: { children: ReactNode; className?: string; onClick?: () => void }) {
  return (
    <tr
      onClick={onClick}
      className={cn("transition-colors last:[&>td]:border-b-0", onClick && "cursor-pointer hover:bg-subtle/70", className)}
    >
      {children}
    </tr>
  );
}

// --------------------------------------------------------------------------- misc

export function Stat({ label, value, sub, tone, icon }: {
  label: ReactNode;
  value: ReactNode;
  sub?: ReactNode;
  tone?: Tone;
  icon?: ReactNode;
}) {
  return (
    <Card className="p-4">
      <div className="flex items-center justify-between text-xs text-muted">
        <span>{label}</span>
        {icon && <span className={cn(tone ? tones[tone].split(" ")[1] : "text-faint")}>{icon}</span>}
      </div>
      <div className="mt-1.5 text-2xl font-semibold tracking-tight tabular-nums">{value}</div>
      {sub && <div className="mt-0.5 text-xs text-faint">{sub}</div>}
    </Card>
  );
}

export function Mono({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={cn("font-mono text-[12px]", className)}>{children}</span>;
}
