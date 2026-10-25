import * as RDialog from "@radix-ui/react-dialog";
import * as RDropdown from "@radix-ui/react-dropdown-menu";
import * as RPopover from "@radix-ui/react-popover";
import * as RSwitch from "@radix-ui/react-switch";
import * as RTabs from "@radix-ui/react-tabs";
import * as RTooltip from "@radix-ui/react-tooltip";
import { X } from "lucide-react";
import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from "react";
import { cn } from "@/lib/cn";
import { Button } from "./primitives";

// --------------------------------------------------------------------------- dialog

export function Dialog({ open, onOpenChange, title, description, children, footer, size = "md", className }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  size?: "sm" | "md" | "lg" | "xl";
  className?: string;
}) {
  const width = { sm: "max-w-sm", md: "max-w-lg", lg: "max-w-2xl", xl: "max-w-4xl" }[size];
  return (
    <RDialog.Root open={open} onOpenChange={onOpenChange}>
      <RDialog.Portal>
        <RDialog.Overlay className="fixed inset-0 z-50 animate-fade-in bg-black/40 backdrop-blur-[2px]" />
        <RDialog.Content
          className={cn(
            "fixed top-[12vh] left-1/2 z-50 flex max-h-[80vh] w-[calc(100vw-2rem)] -translate-x-1/2 animate-pop-in flex-col rounded-2xl border border-border bg-raised shadow-pop",
            width,
            className,
          )}
        >
          <div className="flex items-start justify-between gap-4 border-b border-border px-5 py-4">
            <div>
              <RDialog.Title className="text-[15px] font-semibold">{title}</RDialog.Title>
              {description ? (
                <RDialog.Description className="mt-1 text-[13px] text-muted">{description}</RDialog.Description>
              ) : (
                <RDialog.Description className="sr-only">{typeof title === "string" ? title : "dialog"}</RDialog.Description>
              )}
            </div>
            <RDialog.Close asChild>
              <Button variant="ghost" size="icon-sm" aria-label="Close">
                <X className="size-4" />
              </Button>
            </RDialog.Close>
          </div>
          {children && <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>}
          {footer && <div className="flex justify-end gap-2 border-t border-border px-5 py-3">{footer}</div>}
        </RDialog.Content>
      </RDialog.Portal>
    </RDialog.Root>
  );
}

/** A right-hand sheet for detail views and editors. */
export function Sheet({ open, onOpenChange, title, description, children, footer, width = "max-w-xl" }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  width?: string;
}) {
  return (
    <RDialog.Root open={open} onOpenChange={onOpenChange}>
      <RDialog.Portal>
        <RDialog.Overlay className="fixed inset-0 z-50 animate-fade-in bg-black/30" />
        <RDialog.Content
          className={cn(
            "fixed inset-y-0 right-0 z-50 flex w-full animate-fade-in flex-col border-l border-border bg-raised shadow-pop",
            width,
          )}
        >
          <div className="flex items-start justify-between gap-4 border-b border-border px-5 py-4">
            <div className="min-w-0">
              <RDialog.Title className="truncate text-[15px] font-semibold">{title}</RDialog.Title>
              <RDialog.Description className={description ? "mt-1 text-[13px] text-muted" : "sr-only"}>
                {description ?? "details"}
              </RDialog.Description>
            </div>
            <RDialog.Close asChild>
              <Button variant="ghost" size="icon-sm" aria-label="Close">
                <X className="size-4" />
              </Button>
            </RDialog.Close>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
          {footer && <div className="flex justify-end gap-2 border-t border-border px-5 py-3">{footer}</div>}
        </RDialog.Content>
      </RDialog.Portal>
    </RDialog.Root>
  );
}

// --------------------------------------------------------------------------- confirm()

interface ConfirmOptions {
  title: ReactNode;
  description?: ReactNode;
  confirmLabel?: string;
  danger?: boolean;
  /** Ask the user to type this to confirm (for destructive actions). */
  typeToConfirm?: string;
}

const ConfirmCtx = createContext<(o: ConfirmOptions) => Promise<boolean>>(async () => false);

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [opts, setOpts] = useState<ConfirmOptions | null>(null);
  const [typed, setTyped] = useState("");
  const resolver = useRef<(v: boolean) => void>(() => {});
  const confirm = useCallback((o: ConfirmOptions) => {
    setOpts(o);
    setTyped("");
    return new Promise<boolean>((resolve) => {
      resolver.current = resolve;
    });
  }, []);
  const close = (v: boolean) => {
    resolver.current(v);
    setOpts(null);
  };
  const blocked = !!opts?.typeToConfirm && typed !== opts.typeToConfirm;
  return (
    <ConfirmCtx.Provider value={confirm}>
      {children}
      <Dialog
        open={!!opts}
        onOpenChange={(o) => !o && close(false)}
        title={opts?.title}
        description={opts?.description}
        size="sm"
        footer={
          <>
            <Button variant="ghost" onClick={() => close(false)}>
              Cancel
            </Button>
            <Button variant={opts?.danger ? "danger" : "primary"} disabled={blocked} onClick={() => close(true)} autoFocus>
              {opts?.confirmLabel ?? "Confirm"}
            </Button>
          </>
        }
      >
        {opts?.typeToConfirm && (
          <div>
            <p className="mb-2 text-[13px] text-muted">
              Type <span className="font-mono font-semibold text-fg">{opts.typeToConfirm}</span> to confirm.
            </p>
            <input
              autoFocus
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              className="h-8 w-full rounded-lg border border-border bg-panel px-2.5 font-mono text-xs focus:border-ring focus:outline-none"
            />
          </div>
        )}
      </Dialog>
    </ConfirmCtx.Provider>
  );
}

export function useConfirm() {
  return useContext(ConfirmCtx);
}

// --------------------------------------------------------------------------- menu

export const Menu = RDropdown.Root;
export const MenuTrigger = RDropdown.Trigger;

export function MenuContent({ children, align = "end", className }: {
  children: ReactNode;
  align?: "start" | "end" | "center";
  className?: string;
}) {
  return (
    <RDropdown.Portal>
      <RDropdown.Content
        align={align}
        sideOffset={6}
        className={cn(
          "z-50 min-w-44 animate-pop-in rounded-xl border border-border bg-raised p-1 text-[13px] shadow-pop",
          className,
        )}
      >
        {children}
      </RDropdown.Content>
    </RDropdown.Portal>
  );
}

export function MenuItem({ children, onSelect, icon, danger, shortcut, disabled }: {
  children: ReactNode;
  onSelect?: () => void;
  icon?: ReactNode;
  danger?: boolean;
  shortcut?: ReactNode;
  disabled?: boolean;
}) {
  return (
    <RDropdown.Item
      disabled={disabled}
      onSelect={onSelect}
      className={cn(
        "flex h-8 cursor-pointer items-center gap-2 rounded-lg px-2 outline-none select-none data-[disabled]:pointer-events-none data-[disabled]:opacity-50 data-[highlighted]:bg-subtle",
        danger ? "text-bad" : "text-fg",
      )}
    >
      {icon && <span className="flex size-4 items-center justify-center text-muted [&>svg]:size-4">{icon}</span>}
      <span className="flex-1">{children}</span>
      {shortcut && <span className="text-[11px] text-faint">{shortcut}</span>}
    </RDropdown.Item>
  );
}

export function MenuLabel({ children }: { children: ReactNode }) {
  return <RDropdown.Label className="px-2 pt-1.5 pb-1 text-[11px] font-semibold tracking-wide text-faint uppercase">{children}</RDropdown.Label>;
}

export function MenuSeparator() {
  return <RDropdown.Separator className="my-1 h-px bg-border" />;
}

// --------------------------------------------------------------------------- popover

export function Popover({ trigger, children, align = "start", className, open, onOpenChange }: {
  trigger: ReactNode;
  children: ReactNode;
  align?: "start" | "end" | "center";
  className?: string;
  open?: boolean;
  onOpenChange?: (o: boolean) => void;
}) {
  return (
    <RPopover.Root open={open} onOpenChange={onOpenChange}>
      <RPopover.Trigger asChild>{trigger}</RPopover.Trigger>
      <RPopover.Portal>
        <RPopover.Content
          align={align}
          sideOffset={6}
          className={cn("z-50 animate-pop-in rounded-xl border border-border bg-raised p-3 shadow-pop", className)}
        >
          {children}
        </RPopover.Content>
      </RPopover.Portal>
    </RPopover.Root>
  );
}

// --------------------------------------------------------------------------- tooltip

export const TooltipProvider = RTooltip.Provider;

export function Tooltip({ content, children, side = "top" }: {
  content: ReactNode;
  children: ReactNode;
  side?: "top" | "bottom" | "left" | "right";
}) {
  if (!content) return <>{children}</>;
  return (
    <RTooltip.Root delayDuration={300}>
      <RTooltip.Trigger asChild>{children}</RTooltip.Trigger>
      <RTooltip.Portal>
        <RTooltip.Content
          side={side}
          sideOffset={6}
          className="z-[60] max-w-xs animate-fade-in rounded-md bg-fg px-2 py-1 text-xs text-bg shadow-pop"
        >
          {content}
        </RTooltip.Content>
      </RTooltip.Portal>
    </RTooltip.Root>
  );
}

// --------------------------------------------------------------------------- tabs

export const Tabs = RTabs.Root;
export const TabsContent = RTabs.Content;

export function TabsList({ children, className }: { children: ReactNode; className?: string }) {
  return <RTabs.List className={cn("flex items-center gap-1 border-b border-border", className)}>{children}</RTabs.List>;
}

export function TabsTrigger({ value, children, count }: { value: string; children: ReactNode; count?: number }) {
  return (
    <RTabs.Trigger
      value={value}
      className="relative -mb-px flex h-9 items-center gap-1.5 border-b-2 border-transparent px-2.5 text-[13px] font-medium text-muted transition-colors hover:text-fg data-[state=active]:border-accent data-[state=active]:text-fg"
    >
      {children}
      {count != null && <span className="rounded-full bg-subtle px-1.5 text-[11px] text-muted tabular-nums">{count}</span>}
    </RTabs.Trigger>
  );
}

/** Segmented control for small mode switches (Visual / YAML / Split). */
export function Segmented<T extends string>({ value, onChange, options, className }: {
  value: T;
  onChange: (v: T) => void;
  options: { value: T; label: ReactNode; icon?: ReactNode; title?: string }[];
  className?: string;
}) {
  return (
    <div className={cn("inline-flex h-8 items-center rounded-lg border border-border bg-subtle p-0.5", className)}>
      {options.map((o) => (
        <button
          key={o.value}
          title={o.title}
          onClick={() => onChange(o.value)}
          className={cn(
            "flex h-full items-center gap-1.5 rounded-md px-2.5 text-xs font-medium transition-colors",
            value === o.value ? "bg-panel text-fg shadow-card" : "text-muted hover:text-fg",
          )}
        >
          {o.icon}
          {o.label}
        </button>
      ))}
    </div>
  );
}

// --------------------------------------------------------------------------- switch

export function Switch({ checked, onCheckedChange, disabled, label, id }: {
  checked: boolean;
  onCheckedChange: (v: boolean) => void;
  disabled?: boolean;
  label?: ReactNode;
  id?: string;
}) {
  return (
    <label className={cn("inline-flex items-center gap-2 text-[13px]", disabled ? "opacity-60" : "cursor-pointer")}>
      <RSwitch.Root
        id={id}
        checked={checked}
        onCheckedChange={onCheckedChange}
        disabled={disabled}
        className="relative h-[18px] w-8 shrink-0 rounded-full bg-border-strong transition-colors data-[state=checked]:bg-accent"
      >
        <RSwitch.Thumb className="block size-3.5 translate-x-0.5 rounded-full bg-white shadow transition-transform data-[state=checked]:translate-x-[16px]" />
      </RSwitch.Root>
      {label}
    </label>
  );
}
