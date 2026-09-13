import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

/* ---------------------------------------------------------------
   主题：浅色默认，`.dark` class 切换；持久化到 localStorage。
---------------------------------------------------------------- */

interface ThemeCtx {
  dark: boolean;
  toggle: () => void;
}

const Ctx = createContext<ThemeCtx>({ dark: false, toggle: () => {} });

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [dark, setDark] = useState<boolean>(() =>
    typeof document !== "undefined"
      ? document.documentElement.classList.contains("dark")
      : false,
  );

  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    try {
      localStorage.setItem("agsk-theme", dark ? "dark" : "light");
    } catch {
      /* ignore */
    }
  }, [dark]);

  const value = useMemo<ThemeCtx>(
    () => ({ dark, toggle: () => setDark((d) => !d) }),
    [dark],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useTheme() {
  return useContext(Ctx);
}

/* ---------------------------------------------------------------
   ECharts 调色板：跟随主题，克制配色
---------------------------------------------------------------- */

export interface ChartPalette {
  text: string;
  textDim: string;
  line: string;
  split: string;
  up: string;
  down: string;
  accent: string;
  accentSoft: string;
  gray: string;
  warn: string;
  tooltipBg: string;
  tooltipBorder: string;
}

export function chartPalette(dark: boolean): ChartPalette {
  return dark
    ? {
        text: "#9a9a94",
        textDim: "#6f6f6a",
        line: "#3a3e44",
        split: "#26292e",
        up: "#e0524e",
        down: "#4caf7d",
        accent: "#8fb0d9",
        accentSoft: "rgba(143,176,217,0.08)",
        gray: "#8a8a85",
        warn: "#d9a441",
        tooltipBg: "rgba(28,30,34,0.96)",
        tooltipBorder: "#3a3e44",
      }
    : {
        text: "#5c5c57",
        textDim: "#8a8a85",
        line: "#d4d4cf",
        split: "#ececea",
        up: "#c2312e",
        down: "#1a7f4b",
        accent: "#1f3a5f",
        accentSoft: "rgba(31,58,95,0.06)",
        gray: "#8a8a85",
        warn: "#b45309",
        tooltipBg: "rgba(255,255,255,0.97)",
        tooltipBorder: "#e4e4e0",
      };
}
