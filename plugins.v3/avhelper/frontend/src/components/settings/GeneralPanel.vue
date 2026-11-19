<script setup lang="ts">
/**
 * 设置台 · 常规。
 *
 * 切到 `vue` 渲染模式后宿主不再画 `get_form()` 那套 JSON 配置表单，
 * `enabled` / `proxy` / `javdb_bin` / `data_root` 只能在设置台里改 —— 这一栏是它们唯一的入口。
 * 数据归属：宿主配置，点「保存修改」时走 `emit('save', config)`。
 */
import { computed, onMounted, ref } from 'vue'
import { hostToast, unwrap, type PluginApi } from '../../composables/usePluginApi'
import type { HostConfigDraft } from '../../composables/settingsModel'

const props = defineProps<{
  config: HostConfigDraft
  enabledKnown: boolean
  /** 宿主请求器（壳已补插件 id）：javdb-cli 版本检测/升级用它；拿不到就整段隐藏。 */
  api?: PluginApi | null
  toast?: unknown
}>()

const emit = defineEmits<{
  /** 打补丁到宿主配置草稿（只传改动的字段）。 */
  (event: 'patch', patch: Partial<HostConfigDraft>): void
}>()

function patch<K extends keyof HostConfigDraft>(key: K, value: HostConfigDraft[K]): void {
  emit('patch', { [key]: value } as Partial<HostConfigDraft>)
}

// ------------------------------------------------ javdb-cli 版本（检测更新 → 用户决定）

interface JavdbVersionInfo {
  installed?: string
  pinned?: string
  floor?: string
  latest?: string | null
  checkReason?: string
  checkedAt?: string
  managed?: boolean
  ready?: boolean
}

const versionInfo = ref<JavdbVersionInfo | null>(null)
const versionLoading = ref(false)
const versionError = ref('')
const upgrading = ref(false)
const t = hostToast(props.toast as any)

async function getJson(path: string): Promise<any> {
  const api = props.api
  if (!api || typeof api.get !== 'function') throw new Error('宿主 api 不可用')
  const payload: any = unwrap<any>(await api.get(path))
  if (payload && typeof payload === 'object' && payload.success === false) {
    throw new Error(String(payload.message || '接口返回失败'))
  }
  return payload
}

async function postJson(path: string, body: Record<string, unknown>): Promise<any> {
  const api = props.api
  if (!api || typeof api.post !== 'function') throw new Error('宿主 api 不可用')
  const payload: any = unwrap<any>(await api.post(path, body))
  if (payload && typeof payload === 'object' && payload.success === false) {
    throw new Error(String(payload.message || '接口返回失败'))
  }
  return payload
}

/** 逐段数值比较：installed < latest 才算「有新版」（0.9.0 在手时上游 0.8.4 不提示）。 */
function versionLt(a: string, b: string): boolean {
  const pa = String(a).split('.').map(Number)
  const pb = String(b).split('.').map(Number)
  const width = Math.max(pa.length, pb.length)
  for (let index = 0; index < width; index++) {
    const left = pa[index] || 0
    const right = pb[index] || 0
    if (left !== right) return left < right
  }
  return false
}

const outdated = computed(() => {
  const info = versionInfo.value
  if (!info?.installed || !info?.latest) return false
  return versionLt(info.installed, info.latest)
})

async function loadVersion(force = false): Promise<void> {
  versionLoading.value = true
  versionError.value = ''
  try {
    versionInfo.value = await getJson(`/javdb/version${force ? '?force=1' : ''}`)
  } catch (error) {
    versionError.value = error instanceof Error ? error.message : String(error)
  } finally {
    versionLoading.value = false
  }
}

async function upgrade(): Promise<void> {
  const target = versionInfo.value?.latest || versionInfo.value?.pinned || ''
  if (!target) return
  upgrading.value = true
  try {
    const payload = await postJson('/javdb/upgrade', { version: target })
    if (payload?.success) {
      t.success(String(payload.message || `已升级到 ${target}`))
    } else {
      t.error(String(payload.message || '升级失败'))
    }
    loadVersion(true)
  } catch (error) {
    t.error(error instanceof Error ? error.message : String(error))
  } finally {
    upgrading.value = false
  }
}

onMounted(() => {
  if (props.api) loadVersion(false)
})
</script>

<template>
  <v-card variant="tonal" class="avh-panel">
    <v-card-item>
      <template #prepend>
        <v-icon icon="mdi-cog-outline" color="primary" />
      </template>
      <v-card-title>常规</v-card-title>
      <v-card-subtitle>插件开关、代理与路径 · 属于宿主插件配置</v-card-subtitle>
    </v-card-item>

    <v-card-text>
      <v-switch
        :model-value="props.config.enabled"
        color="primary"
        hide-details
        inset
        label="启用插件"
        class="mb-2"
        @update:model-value="(value: any) => patch('enabled', value === true)"
      />
      <p v-if="!props.enabledKnown" class="text-caption text-medium-emphasis mb-4">
        拿不到当前开关状态（<code>/bootstrap</code> 未返回 <code>enabled</code>），保存前请确认。
      </p>
      <p class="text-caption text-medium-emphasis mb-4">
        关闭后定时订阅检查与「运行一次」都不执行，界面仍可浏览。
      </p>

      <v-text-field
        :model-value="props.config.proxy"
        label="javdb 代理地址"
        placeholder="http://host:port"
        hint="javdb 子进程与站点请求走这个代理；站点自己开了代理时按站点配置走。留空 = 直连。"
        persistent-hint
        variant="outlined"
        density="comfortable"
        clearable
        class="mb-4"
        @update:model-value="(value: any) => patch('proxy', value ?? '')"
      />

      <v-text-field
        :model-value="props.config.javdb_bin"
        label="javdb 路径"
        placeholder="留空 = 自动下载 / 自动查找"
        hint="发现层依赖 javdb-cli 单文件二进制。留空时插件启动后自动下一份到数据目录；网络不通就手工放一份再填这里。"
        persistent-hint
        variant="outlined"
        density="comfortable"
        clearable
        class="mb-4"
        @update:model-value="(value: any) => patch('javdb_bin', value ?? '')"
      />

      <v-text-field
        :model-value="props.config.data_root"
        label="数据目录"
        placeholder="留空 = MP 插件数据目录（/config/plugins/AVHelper）"
        hint="SQLite 台账、JSON 缓存与图片缓存同根。改这里等于换一套数据，旧数据不会自动搬过去。"
        persistent-hint
        variant="outlined"
        density="comfortable"
        clearable
        @update:model-value="(value: any) => patch('data_root', value ?? '')"
      />

      <!-- javdb-cli 版本：检测上游 → 用户决定是否升级（绝不静默换版） -->
      <v-divider class="mb-4" />
      <div class="d-flex align-center mb-1">
        <div class="text-caption text-medium-emphasis">javdb-cli 版本</div>
        <v-spacer />
        <v-btn
          size="small"
          variant="text"
          :loading="versionLoading"
          @click="loadVersion(true)"
        >重新检测</v-btn>
      </div>
      <div v-if="versionError" class="text-caption text-error mb-2">
        检测失败：{{ versionError }}
      </div>
      <div v-else-if="versionInfo" class="text-body-2 mb-1">
        <template v-if="versionInfo.installed">已装 <b>{{ versionInfo.installed }}</b></template>
        <template v-else-if="versionInfo.ready">已就位（版本未知）</template>
        <template v-else>未就绪</template>
        <template v-if="versionInfo.latest">
          · 上游最新 <b>{{ versionInfo.latest }}</b>
          <span class="text-caption text-medium-emphasis">（检测于 {{ versionInfo.checkedAt || '—' }}）</span>
        </template>
        <span v-else-if="versionInfo.checkReason" class="text-caption text-medium-emphasis">
          · 上游检测失败：{{ versionInfo.checkReason }}
        </span>
      </div>
      <v-alert
        v-if="outdated && versionInfo?.managed"
        type="info" variant="tonal" density="compact" class="mb-2"
      >
        上游有新版本 {{ versionInfo?.latest }}（当前 {{ versionInfo?.installed }}）。
        <v-btn
          size="x-small" variant="outlined" color="primary" class="ml-2"
          :loading="upgrading"
          @click="upgrade"
        >升级到 {{ versionInfo?.latest }}</v-btn>
      </v-alert>
      <p
        v-else-if="outdated && versionInfo?.managed === false"
        class="text-caption text-medium-emphasis mb-2"
      >
        上游有新版本 {{ versionInfo?.latest }}，但 javdb 路径是手工指定的——插件不代劳，请自行更换。
      </p>
      <p class="text-caption text-medium-emphasis">
        版本低于插件下限（{{ versionInfo?.floor || '' }}）时启动会自动升级，无需手动操作。
      </p>
    </v-card-text>
  </v-card>
</template>

<style scoped>
.avh-panel {
  margin-bottom: 12px;
}
</style>
