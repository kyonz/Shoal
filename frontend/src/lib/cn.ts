import { clsx, type ClassValue } from "clsx";
import { extendTailwindMerge } from "tailwind-merge";

// Teach tailwind-merge the custom theme tokens so e.g. `text-muted` and `text-xs` don't collide.
const merge = extendTailwindMerge({
  extend: {
    theme: {
      color: ["bg", "panel", "raised", "subtle", "fg", "muted", "faint", "border", "border-strong", "accent", "accent-fg", "accent-soft", "ring", "ok", "ok-soft", "warn", "warn-soft", "bad", "bad-soft", "info", "info-soft", "idle", "idle-soft"],
    },
  },
});

/** Join class names; later Tailwind classes override earlier conflicting ones. */
export function cn(...inputs: ClassValue[]) {
  return merge(clsx(inputs));
}
