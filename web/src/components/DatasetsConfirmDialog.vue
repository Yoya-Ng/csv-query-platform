<script setup lang="ts">
// 寫入類操作共用的二次確認對話框：操作人姓名（必填、不預填、不存 localStorage）＋可選的「輸入文字才可按」。
import { computed, onMounted, ref, watch } from "vue";

// 注意：Vue 會把「沒傳的布林 prop」轉成 false（不是 undefined），所以預設 true 必須用 withDefaults。
const props = withDefaults(defineProps<{
  open: boolean;
  title: string;
  confirmLabel: string;
  danger?: boolean;
  busy?: boolean;
  requireOperator?: boolean;   // 預設 true
  expectText?: string;         // 需輸入的完整文字（例如期別）
  error?: string;
}>(), { requireOperator: true, danger: false, busy: false, expectText: undefined, error: "" });
const emit = defineEmits<{ (e: "confirm", operator: string): void; (e: "cancel"): void }>();

const dlg = ref<HTMLDialogElement | null>(null);
const operator = ref("");
const typed = ref("");

function sync(v: boolean) {
  const d = dlg.value;
  if (!d) return;
  if (v && !d.open) {
    operator.value = "";
    typed.value = "";
    d.showModal();
  } else if (!v && d.open) {
    d.close();
  }
}
watch(() => props.open, sync);
onMounted(() => sync(props.open));

const needOperator = computed(() => props.requireOperator !== false);
const ok = computed(() =>
  (!needOperator.value || operator.value.trim().length > 0) &&
  (props.expectText === undefined || typed.value.trim() === props.expectText));
function submit() {
  if (ok.value && !props.busy) emit("confirm", operator.value.trim());
}
</script>

<template>
  <dialog ref="dlg" class="modal" @cancel.prevent="emit('cancel')">
    <form class="stack" @submit.prevent="submit">
      <h2>{{ title }}</h2>
      <slot />
      <div v-if="expectText !== undefined" class="field">
        <label>請輸入 {{ expectText }} 以確認</label>
        <input v-model="typed" type="text" autocomplete="off" inputmode="numeric" />
      </div>
      <div v-if="needOperator" class="field">
        <label>操作人姓名（必填）</label>
        <input v-model="operator" type="text" autocomplete="off" maxlength="50" />
        <span class="small muted">僅作為操作註記，不是身分驗證；系統不核對姓名真偽（SPEC 11）。</span>
      </div>
      <div v-if="error" class="notice error">{{ error }}</div>
      <div class="row end">
        <button type="button" class="btn" :disabled="busy" @click="emit('cancel')">取消</button>
        <button type="submit" class="btn" :class="danger ? 'danger' : 'primary'" :disabled="!ok || busy">
          {{ busy ? "處理中…" : confirmLabel }}
        </button>
      </div>
    </form>
  </dialog>
</template>
