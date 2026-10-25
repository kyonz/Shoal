import { useEffect, useState } from "react";

export type Theme = "light" | "dark" | "system";

function apply(theme: Theme) {
  const dark = theme === "dark" || (theme === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.classList.toggle("dark", dark);
}

export function useTheme(): [Theme, (t: Theme) => void] {
  const [theme, setTheme] = useState<Theme>(() => {
    try {
      return (localStorage.getItem("shoal.theme") as Theme) || "system";
    } catch {
      return "system";
    }
  });
  useEffect(() => {
    apply(theme);
    try {
      localStorage.setItem("shoal.theme", theme);
    } catch {
      /* ignore */
    }
    if (theme !== "system") return;
    const mq = matchMedia("(prefers-color-scheme: dark)");
    const on = () => apply("system");
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, [theme]);
  return [theme, setTheme];
}
