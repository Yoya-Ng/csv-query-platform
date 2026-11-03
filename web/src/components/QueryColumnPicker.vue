<script setup lang="ts">
// 欄位挑選（SPEC 9.4）：可搜尋、依群組勾選、調整順序、儲存常用欄位組合（只存在本機瀏覽器）。
import { computed, ref } from "vue";
import type { Dataset } from "../lib/api";
import { MAX_OUTPUT_COLS } from "../lib/querySpec";

const props = defineProps<{ datasets: Dataset[]; modelValue: string[]; calcCount: number }>();
const emit = defineEmits<{ "update:modelValue": [string[]] }>();

const search = ref("");
const open = ref<Record<string, boolean>>({});

const selected = computed(() => new Set(props.modelValue));
const remaining = computed(() => MAX_OUTPUT_COLS - props.modelValue.length - props.calcCount);

interface Item { ref: string; code: string; label: string; type: string; pk: boolean }
const groups = computed(() => {
  const q = search.value.trim().toLowerCase();
  const out: { key: string; table: string; group: string; items: Item[] }[] = [];
  for (const ds of props.datasets) {
    const byGroup = new Map<string, Item[]>();
    for (const c of ds.columns) {
      if (c.disabled) continue;
      const hay = `${c.code} ${c.display_name} ${c.aliases.join(" ")} ${c.description}`.toLowerCase();
      if (q && !hay.includes(q)) continue;
      const g = c.group_name || "未分組";
      if (!byGroup.has(g)) byGroup.set(g, []);
      byGroup.get(g)!.push({ ref: `${ds.name}.${c.code}`, code: c.code, label: c.display_name, type: c.data_type, pk: c.is_pk });
    }
    for (const [g, items] of byGroup) out.push({ key: `${ds.name}::${g}`, table: ds.name, group: g, items });
  }
  return out;
});

function isOpen(key: string) {
  return search.value.trim() !== "" || open.value[key] === true;
}
function set(list: string[]) {
  emit("update:modelValue", list);
}
function toggle(r: string) {
  if (selected.value.has(r)) set(props.modelValue.filter((x) => x !== r));
  else if (remaining.value > 0) set([...props.modelValue, r]);
}
function toggleGroup(items: Item[], on: boolean) {
  if (!on) {
    const drop = new Set(items.map((i) => i.ref));
    set(props.modelValue.filter((x) => !drop.has(x)));
    return;
  }
  const add = items.map((i) => i.ref).filter((r) => !selected.value.has(r)).slice(0, Math.max(remaining.value, 0));
  set([...props.modelValue, ...add]);
}
function move(i: number, d: number) {
  const j = i + d;
  if (j < 0 || j >= props.modelValue.length) return;
  const list = [...props.modelValue];
  [list[i], list[j]] = [list[j], list[i]];
  set(list);
}

// 常用欄位組合：localStorage（per-viewer 便利功能；讀寫失敗時忽略）
const KEY = "cqp-column-sets";
type Sets = Record<string, string[]>;
function readSets(): Sets {
  try { return JSON.parse(localStorage.getItem(KEY) ?? "{}") as Sets; } catch { return {}; }
}
const sets = ref<Sets>(readSets());
const setName = ref("");
function saveSet() {
  const n = setName.value.trim();
  if (!n || !props.modelValue.length) return;
  sets.value = { ...sets.value, [n]: [...props.modelValue] };
  try { localStorage.setItem(KEY, JSON.stringify(sets.value)); } catch { /* 忽略 */ }
  setName.value = "";
}
function applySet(n: string) {
  const known = new Set(props.datasets.flatMap((d) => d.columns.filter((c) => !c.disabled).map((c) => `${d.name}.${c.code}`)));
  set((sets.value[n] ?? []).filter((r) => known.has(r)).slice(0, MAX_OUTPUT_COLS - props.calcCount));
}
function deleteSet(n: string) {
  const next = { ...sets.value };
  delete next[n];
  sets.value = next;
  try { localStorage.setItem(KEY, JSON.stringify(next)); } catch { /* 忽略 */ }
}
</script>

<template>
  <div class="picker">
    <div class="row">
      <input v-model="search" type="search" class="grow" placeholder="搜尋欄位（實體名、顯示名稱、別名、說明）" aria-label="搜尋欄位" />
      <span class="badge" :class="remaining <= 0 ? 'warn' : ''">已選 {{ modelValue.length + calcCount }} / {{ MAX_OUTPUT_COLS }}</span>
    </div>

    <div class="lists">
      <div class="available" aria-label="可選欄位">
        <p v-if="!groups.length" class="muted small">沒有符合的欄位</p>
        <section v-for="g in groups" :key="g.key" class="group">
          <div class="group-head">
            <button type="button" class="link" :aria-expanded="isOpen(g.key)" @click="open[g.key] = !isOpen(g.key)">
              {{ isOpen(g.key) ? "▾" : "▸" }} <span class="mono">{{ g.table }}</span> · {{ g.group }}
              <span class="muted small">（{{ g.items.length }}）</span>
            </button>
            <span class="row">
              <button type="button" class="btn sm ghost" :disabled="remaining <= 0" @click="toggleGroup(g.items, true)">全選</button>
              <button type="button" class="btn sm ghost" @click="toggleGroup(g.items, false)">清除</button>
            </span>
          </div>
          <ul v-if="isOpen(g.key)" class="items">
            <li v-for="it in g.items" :key="it.ref">
              <label class="item">
                <input type="checkbox" :checked="selected.has(it.ref)"
                       :disabled="!selected.has(it.ref) && remaining <= 0" @change="toggle(it.ref)" />
                <span class="mono">{{ it.code }}</span>
                <span v-if="it.label" class="muted small ellipsis">{{ it.label }}</span>
                <span class="muted small type">{{ it.type }}<template v-if="it.pk"> · 主鍵</template></span>
              </label>
            </li>
          </ul>
        </section>
      </div>

      <div class="chosen" aria-label="已選欄位（輸出順序）">
        <div class="label">輸出順序</div>
        <p v-if="!modelValue.length" class="muted small">尚未選擇欄位</p>
        <ol class="items">
          <li v-for="(r, i) in modelValue" :key="r" class="chosen-item">
            <span class="ellipsis grow" :title="r">
              <span class="mono">{{ r.slice(r.indexOf('.') + 1) }}</span>
              <span class="muted small"> · {{ r.slice(0, r.indexOf('.')) }}</span>
            </span>
            <button type="button" class="btn sm ghost" :disabled="i === 0" aria-label="上移" @click="move(i, -1)">↑</button>
            <button type="button" class="btn sm ghost" :disabled="i === modelValue.length - 1" aria-label="下移" @click="move(i, 1)">↓</button>
            <button type="button" class="btn sm ghost" aria-label="移除" @click="toggle(r)">✕</button>
          </li>
        </ol>
        <div class="sets">
          <div class="label">常用欄位組合（只存在這台電腦的瀏覽器）</div>
          <div class="row">
            <input v-model="setName" class="grow" placeholder="組合名稱" aria-label="組合名稱" />
            <button type="button" class="btn sm" :disabled="!setName.trim() || !modelValue.length" @click="saveSet">儲存</button>
          </div>
          <div v-for="(_, n) in sets" :key="n" class="row">
            <button type="button" class="btn sm ghost grow set-btn" @click="applySet(String(n))">{{ n }}</button>
            <button type="button" class="btn sm ghost" :aria-label="`刪除組合 ${n}`" @click="deleteSet(String(n))">✕</button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.picker { display: flex; flex-direction: column; gap: 8px; }
/* 建構器欄寬約 440px，兩欄並排會把欄名截斷；一律上下排列 */
.lists { display: grid; grid-template-columns: minmax(0, 1fr); gap: 8px; }
.available, .chosen { border: 1px solid var(--border); border-radius: 6px; padding: 6px; overflow: auto; min-width: 0; }
.available { max-height: 320px; }
.chosen { max-height: 260px; }
.group + .group { border-top: 1px solid var(--border); margin-top: 4px; padding-top: 4px; }
.group-head { display: flex; justify-content: space-between; align-items: center; gap: 6px; flex-wrap: wrap; }
.link { background: none; border: none; padding: 2px 0; cursor: pointer; text-align: left; color: var(--text); }
.items { list-style: none; margin: 0; padding: 0; }
.item { display: flex; gap: 6px; align-items: center; padding: 2px 4px; border-radius: 4px; cursor: pointer; min-width: 0; }
.item:hover { background: var(--surface-2); }
.ellipsis { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
.type { margin-left: auto; white-space: nowrap; }
.chosen-item { display: flex; gap: 4px; align-items: center; padding: 1px 0; min-width: 0; }
.sets { margin-top: 8px; display: flex; flex-direction: column; gap: 4px; }
.set-btn { justify-content: flex-start; overflow: hidden; text-overflow: ellipsis; }
</style>
