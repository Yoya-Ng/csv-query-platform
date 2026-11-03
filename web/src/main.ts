import { createApp } from "vue";
import { createRouter, createWebHashHistory } from "vue-router";
import { AllCommunityModule, ModuleRegistry } from "ag-grid-community";
import App from "./App.vue";
import "./styles.css";

// AG Grid v33+ 需先註冊模組（只用 Community 版，MIT 授權）
ModuleRegistry.registerModules([AllCommunityModule]);

// hash 路由：由 FastAPI 提供靜態檔時不需要伺服器端 fallback
const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: "/", redirect: "/query" },
    { path: "/query", component: () => import("./views/QueryView.vue"), meta: { title: "查詢" } },
    { path: "/import", component: () => import("./views/ImportView.vue"), meta: { title: "匯入" } },
    { path: "/loads", component: () => import("./views/LoadsView.vue"), meta: { title: "匯入紀錄" } },
    { path: "/datasets", component: () => import("./views/DatasetsView.vue"), meta: { title: "資料集與欄位" } },
  ],
});
router.afterEach((to) => {
  document.title = `${(to.meta.title as string) ?? ""}｜CSV 查詢平台`;
});

createApp(App).use(router).mount("#app");
