// AG Grid 共用主題：沿用全站 CSS 變數，深淺色自動跟著 :root 的 token 切換。
import { themeQuartz } from "ag-grid-community";

export const gridTheme = themeQuartz.withParams({
  backgroundColor: "var(--surface)",
  foregroundColor: "var(--text)",
  headerBackgroundColor: "var(--surface-2)",
  borderColor: "var(--border)",
  accentColor: "var(--accent)",
  chromeBackgroundColor: "var(--surface-2)",
  oddRowBackgroundColor: "color-mix(in srgb, var(--surface-2) 45%, var(--surface))",
  fontFamily: "var(--font)",
  fontSize: 13,
  rowHeight: 30,
  headerHeight: 34,
  spacing: 6,
});

/** 後端欄名 `表__欄` 轉成顯示用標題。 */
export function headerOf(col: string, displayNames: Record<string, string> = {}): string {
  if (displayNames[col]) return displayNames[col];
  const i = col.indexOf("__");
  return i > 0 ? `${col.slice(0, i)}.${col.slice(i + 2)}` : col;
}
