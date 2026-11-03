<script setup lang="ts">
import { ref } from "vue";

const links = [
  { to: "/query", label: "查詢" },
  { to: "/import", label: "匯入" },
  { to: "/loads", label: "匯入紀錄" },
  { to: "/datasets", label: "資料集與欄位" },
];

// 主題：預設跟隨系統，可手動切換（只存在本機瀏覽器）
type Theme = "auto" | "light" | "dark";
function readTheme(): Theme {
  try {
    const t = localStorage.getItem("cqp-theme");
    return t === "light" || t === "dark" ? t : "auto";
  } catch {
    return "auto";
  }
}
const theme = ref<Theme>(readTheme());
function applyTheme() {
  if (theme.value === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", theme.value);
  try { localStorage.setItem("cqp-theme", theme.value); } catch { /* 私密模式 */ }
}
applyTheme();
function cycleTheme() {
  theme.value = theme.value === "auto" ? "light" : theme.value === "light" ? "dark" : "auto";
  applyTheme();
}
const themeLabel = { auto: "主題：跟隨系統", light: "主題：淺色", dark: "主題：深色" };
</script>

<template>
  <header class="topbar">
    <div class="brand">CSV 查詢平台</div>
    <nav class="nav" aria-label="主要功能">
      <RouterLink v-for="l in links" :key="l.to" :to="l.to" class="nav-link">{{ l.label }}</RouterLink>
    </nav>
    <button class="btn ghost sm theme" type="button" @click="cycleTheme">{{ themeLabel[theme] }}</button>
  </header>
  <main>
    <RouterView />
  </main>
</template>

<style scoped>
.topbar {
  display: flex; align-items: center; gap: 16px; padding: 8px 16px;
  background: var(--surface); border-bottom: 1px solid var(--border); position: sticky; top: 0; z-index: 10;
  flex-wrap: wrap;
}
.brand { font-weight: 700; white-space: nowrap; }
.nav { display: flex; gap: 4px; flex: 1 1 auto; overflow-x: auto; min-width: 0; }
.nav-link {
  padding: 6px 10px; border-radius: 6px; color: var(--text-muted); text-decoration: none; white-space: nowrap;
}
.nav-link:hover { background: var(--surface-2); color: var(--text); }
.nav-link.router-link-active { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
@media (max-width: 600px) {
  .topbar { gap: 8px; padding: 8px 12px; }
  .brand { flex: 1 1 auto; }
  .nav { order: 3; flex-basis: 100%; }
}
</style>
