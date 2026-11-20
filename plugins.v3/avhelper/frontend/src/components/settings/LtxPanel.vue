<script setup lang="ts">
/**
 * 设置台 · LTX 库（2026-11-20 新源）。
 *
 * Jasna-LTX 破解资源库的接入面：开关、库地址、FlareSolverr（过 CF 盾收 cookies）、
 * 重爬节奏，外加本地快照状态与「立即重爬」。数据归属：`ltx*` 键与「下载规则」
 * 同住 rule 草稿（后端 `ltx_conf()` 读同一份 kv），点「保存修改」时由壳统一提交。
 *
 * 状态区走 `GET /ltx/status`（不依赖 rule 草稿），重爬走 `POST /ltx/refresh`
 * （后端丢工作线程，返回时快照已更新）。写法照 ActorPanel（同一套 getJson/postJson
 * 与字符串数组 emits —— AGENTS.md §5：函数式 emits 编译出函数对象，事件静默失效）。
 */
import { onMounted, ref } from 'vue'
import { hostToast, unwrap, type PluginApi } from '../../composables/usePluginApi'
import type { RuleState } from '../../composables/settingsModel'

const props = defineProps<{
  rule: RuleState
  api: PluginApi | null
  toast?: unknown
}>()

/** ⚠️ 必须是字符串数组（函数式写法编译出 emits: <函数>，事件静默失效）。 */
const emit = defineEmits(['patch'])

const t = hostToast(props.toast as any)

function patch(key: string, value: unknown): void {
  emit('patch', { [key]: value })
}

function textOf(value: unknown): string {
  return value === null || value === undefined ? '' : String(value)
}

async function getJson(path: string): Promise<any> {
  const api = props.api
  if (!api || typeof api.get !== 'function') throw new Error('宿主 api 不可用')
  const payload: any = unwrap<any>(await api.get(path))
  if (payload && typeof payload === 'object' && payload.success === false) {
    throw new Error(textOf(payload.message) || '接口返回失败')
  }
  return payload
}

async function postJson(path: string, body: Record<string, unknown>): Promise<any> {
  const api = props.api
  if (!api || typeof api.post !== 'function') throw new Error('宿主 api 不可用')
  const payload: any = unwrap<any>(await api.post(path, body))
  if (payload && typeof payload === 'object' && payload.success === false) {
    throw new Error(textOf(payload.message) || '接口返回失败')
  }
  return payload
}

// ---------------------------------------------------------------- 快照状态（GET /ltx/status）

interface LtxStats {
  total?: number
  ok?: number
  pending?: number
  magnet?: number
  ed2k?: number
}

interface LtxStatus {
  ready?: boolean
  stats?: LtxStats
  fetchedAt?: string
  snapshot?: string
}

interface LtxLast {
  ok?: boolean
  reason?: string
  message?: string
  sitekey?: string
  at?: string
  /** 重爬成功时的 diff（后端 `ltxstore.diff()`）：added/removed 为小写番号数组。 */
  delta?: { added?: string[]; removed?: string[]; added_with_link?: number }
}

const status = ref<LtxStatus | null>(null)
const last = ref<LtxLast | null>(null)
const cryptoReady = ref(true)
const statusLoading = ref(false)
const statusError = ref('')
const refreshing = ref(false)

const REASON_TEXT: Record<string, string> = {
  verify: '站点要求人机验证（Turnstile）——需要真实浏览器，自动流程过不了',
  origin: '站点拒绝非网页来源——插件版本过旧（缺 Origin/Referer 伪装）或协议升级',
  cf: 'Cloudflare 拦截——配好 FlareSolverr 后点「立即重爬」再试',
  session: '加密会话建立失败——多为站点协议升级或网络不通',
  crypto: '容器缺 cryptography 库，无法解密 LTX 响应',
  network: '网络不通 / 地址不可达',
  parse: '响应解析失败（协议可能升级）',
  http: '站点返回错误',
}

function reasonText(reason: string | undefined, message: string | undefined): string {
  const label = reason ? (REASON_TEXT[reason] || `原因：${reason}`) : '未知原因'
  return message ? `${label}；${message}` : label
}

async function loadStatus(): Promise<void> {
  statusLoading.value = true
  statusError.value = ''
  try {
    const payload = await getJson('plugin/ltx/status')
    status.value = payload?.status || null
    last.value = payload?.last || null
    cryptoReady.value = payload?.cryptoReady !== false
  } catch (error) {
    statusError.value = error instanceof Error ? error.message : String(error)
  } finally {
    statusLoading.value = false
  }
}

async function refreshNow(): Promise<void> {
  refreshing.value = true
  try {
    const payload = await postJson('plugin/ltx/refresh', {})
    status.value = payload?.status || status.value
    last.value = payload?.result || null
    if (payload?.success) {
      const delta = payload.result?.delta || {}
      const added = Array.isArray(delta.added) ? delta.added.length : 0
      t.success(`重爬完成：新增 ${added} 条（带链接 ${delta.added_with_link ?? 0}）`)
    } else {
      t.error('重爬失败：' + reasonText(payload?.result?.reason, payload?.result?.message))
    }
  } catch (error) {
    t.error(error instanceof Error ? error.message : String(error))
  } finally {
    refreshing.value = false
    loadStatus()
  }
}

onMounted(loadStatus)
</script>

<template>
  <v-card variant="tonal" class="avh-panel">
    <v-card-item>
      <template #prepend>
        <v-icon icon="mdi-movie-open-play-outline" color="primary" />
      </template>
      <v-card-title>LTX 库</v-card-title>
      <v-card-subtitle>破解资源本地库 · 每日重爬一次 · 下载链的 BT 兜底磁力源</v-card-subtitle>
    </v-card-item>

    <v-card-text>
      <p class="text-caption text-medium-emphasis mb-4">
        LTX 库（Jasna-LTX）汇总 LTX 模型破解版资源：插件每天全量重爬一次落到本地，
        之后按番号查磁力、订阅自动下载全部走本地数据，不发任何请求。候选会进
        「下载规则」的 BT 兜底池，与普通破解版一起打分（配合「破解关键字词表」可让
        LTX 版优先）。注意：只收磁力链接，ed2k 资源不参与下载。
      </p>

      <v-switch
        :model-value="props.rule.ltxEnabled !== false"
        color="primary"
        hide-details
        inset
        label="启用 LTX 库"
        class="mb-1"
        @update:model-value="(value: any) => patch('ltxEnabled', value === true)"
      />
      <p class="text-caption text-medium-emphasis mb-4">
        关闭后不重爬、候选池也不注入 LTX 磁力（已落的本地数据保留）。
      </p>

      <v-text-field
        :model-value="textOf(props.rule.ltxSite)"
        label="库地址"
        placeholder="默认 https://ltxlib.dpdns.org"
        hint="换镜像站时才需要改。"
        persistent-hint
        variant="outlined"
        density="comfortable"
        clearable
        class="mb-4"
        @update:model-value="(value: any) => patch('ltxSite', value ?? '')"
      />

      <v-text-field
        :model-value="textOf(props.rule.ltxFlare)"
        label="FlareSolverr 地址"
        placeholder="http://192.168.1.173:8191"
        hint="站点有 Cloudflare 盾：直连被拦时用它过盾收 cookies（只收割 cookies，不放常驻浏览器）。留空 = 直连。"
        persistent-hint
        variant="outlined"
        density="comfortable"
        clearable
        class="mb-4"
        @update:model-value="(value: any) => patch('ltxFlare', value ?? '')"
      />

      <div class="mb-4">
        <div class="text-caption text-medium-emphasis mb-1">重爬间隔</div>
        <v-btn-toggle
          :model-value="props.rule.ltxRefreshDays === 2 ? 2 : 1"
          mandatory
          density="comfortable"
          variant="outlined"
          color="primary"
          @update:model-value="(value: any) => patch('ltxRefreshDays', value)"
        >
          <v-btn :value="1">每天</v-btn>
          <v-btn :value="2">隔天</v-btn>
        </v-btn-toggle>
        <p class="text-caption text-medium-emphasis mt-2">
          每天凌晨 5:13 执行（隔天模式为双日 5:13）；改间隔保存后需重载插件生效。
        </p>
      </div>

      <v-divider class="mb-4" />

      <!-- 状态区 -->
      <div class="d-flex align-center mb-2">
        <div class="text-caption text-medium-emphasis">本地快照状态</div>
        <v-spacer />
        <v-btn
          size="small"
          variant="text"
          color="primary"
          :loading="refreshing"
          :disabled="props.rule.ltxEnabled === false"
          @click="refreshNow"
        >立即重爬</v-btn>
      </div>

      <v-alert v-if="!cryptoReady" type="warning" variant="tonal" density="compact" class="mb-2">
        容器缺少 cryptography 库，无法解密 LTX 响应 —— 请在 MoviePilot 容器里安装后重载插件。
      </v-alert>

      <div v-if="status?.ready" class="text-body-2 mb-2">
        共 <b>{{ status.stats?.total ?? 0 }}</b> 条
        （磁力 {{ status.stats?.magnet ?? 0 }} · ed2k {{ status.stats?.ed2k ?? 0 }} ·
        待确认 {{ status.stats?.pending ?? 0 }}）
        <span class="text-caption text-medium-emphasis">
          · 上次更新 {{ status.fetchedAt || '—' }}
        </span>
      </div>
      <div v-else class="text-body-2 text-medium-emphasis mb-2">
        本地还没有快照 —— 点「立即重爬」抓一份（首次约 10~30 秒）。
      </div>

      <v-alert
        v-if="last && last.ok === false"
        type="warning" variant="tonal" density="compact" class="mb-2"
      >
        上次重爬失败：{{ reasonText(last.reason, last.message) }}
        <template v-if="last.reason === 'verify' && last.sitekey">
          （sitekey {{ last.sitekey }}）
        </template>
      </v-alert>
      <div v-else-if="last && last.ok" class="text-caption text-medium-emphasis mb-2">
        上次重爬 {{ last.at }}：新增 {{ Array.isArray(last.delta?.added) ? last.delta.added.length : 0 }}
        条（带链接 {{ last.delta?.added_with_link ?? 0 }}）、下架 {{ Array.isArray(last.delta?.removed) ? last.delta.removed.length : 0 }} 条
      </div>

      <v-alert v-if="statusError" type="error" variant="tonal" density="compact">
        状态读取失败：{{ statusError }}
      </v-alert>
    </v-card-text>
  </v-card>
</template>
