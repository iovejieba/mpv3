<script setup>
import { formatBytes } from '../utils'
import { formatUploadPerDay, healthShort, healthTone } from '../v9-ui'

defineProps({
  tasks: { type: Array, default: () => [] },
  selectedId: { type: String, default: '' },
  dense: { type: Boolean, default: false },
})
defineEmits(['select', 'create'])

function abnormalCount(task) {
  const download = task.strategy?.ui_summary?.download || {}
  return (download.stalled_count || 0) + (download.slow_count || 0)
    + (download.queued_count || 0) + (download.error_count || 0)
}
</script>

<template>
  <!-- 紧凑列表（任务较多时）：一行一任务，原子载荷全保留——
       状态短形态与结论单行截断均带悬停全文，点击行=选中任务，详情仍由下方面板承载 -->
  <section v-if="dense && tasks.length" class="task-health-rows" aria-label="刷流任务健康状态（紧凑）">
    <button v-for="task in tasks" :key="task.id" type="button" class="task-health-row"
      :class="{ selected: task.id === selectedId }"
      :title="task.strategy?.ui_summary?.health?.message || '首次检查后显示下一步。'"
      @click="$emit('select', task.id)">
      <strong class="task-health-row__name">{{ task.name }}</strong>
      <span class="task-health-row__site">{{ task.site_name }} · {{ task.downloader }}</span>
      <span class="task-health-row__cap">
        <VProgressLinear class="task-health-row__bar"
          :model-value="Math.min(task.strategy?.ui_summary?.capacity?.percent || 0, 100)"
          :color="(task.strategy?.ui_summary?.capacity?.percent || 0) > 100 ? 'error' : healthTone(task.strategy?.ui_summary?.health?.level)"
          height="4" rounded />
        <small>{{ formatBytes(task.seeding_size) }}<template v-if="task.strategy?.ui_summary?.capacity?.limit_bytes"> / {{ formatBytes(task.strategy.ui_summary.capacity.limit_bytes) }}</template></small>
      </span>
      <VChip size="x-small" :color="healthTone(task.strategy?.ui_summary?.health?.level)" variant="tonal"
        :title="task.strategy?.ui_summary?.health?.title || '等待检查'">
        {{ healthShort(task.strategy?.ui_summary?.health?.level) }}
      </VChip>
      <span class="task-health-row__meta">上传 {{ formatUploadPerDay(task.strategy?.uploaded_gb_per_day) }} · 异常 {{ abnormalCount(task) }}</span>
      <span class="task-health-row__msg">{{ task.strategy?.ui_summary?.health?.message || '首次检查后显示下一步。' }}</span>
    </button>
  </section>
  <section v-else class="task-health-grid" aria-label="刷流任务健康状态">
    <button v-for="task in tasks" :key="task.id" type="button" class="task-health-card"
      :class="{ selected: task.id === selectedId }" @click="$emit('select', task.id)">
      <div class="task-health-card__top">
        <div><strong>{{ task.name }}</strong><span>{{ task.site_name }} · {{ task.downloader }}</span></div>
        <VChip size="x-small" :color="healthTone(task.strategy?.ui_summary?.health?.level)" variant="tonal">
          {{ task.strategy?.ui_summary?.health?.title || '等待检查' }}
        </VChip>
      </div>
      <div class="task-health-card__capacity">
        <span>{{ formatBytes(task.seeding_size) }}</span>
        <small>{{ task.strategy?.ui_summary?.capacity?.limit_bytes ? `上限 ${formatBytes(task.strategy.ui_summary.capacity.limit_bytes)}` : '容量未设置' }}</small>
      </div>
      <VProgressLinear :model-value="Math.min(task.strategy?.ui_summary?.capacity?.percent || 0, 100)"
        :color="(task.strategy?.ui_summary?.capacity?.percent || 0) > 100 ? 'error' : healthTone(task.strategy?.ui_summary?.health?.level)" height="5" rounded />
      <div class="task-health-card__meta">
        <span>上传 {{ formatUploadPerDay(task.strategy?.uploaded_gb_per_day) }}</span>
        <span>异常 {{ abnormalCount(task) }}</span>
      </div>
      <p>{{ task.strategy?.ui_summary?.health?.message || '首次检查后显示下一步。' }}</p>
    </button>
    <button v-if="!tasks.length" type="button" class="task-health-card task-health-card--create" @click="$emit('create')">
      <VIcon icon="mdi-plus-circle-outline" size="24" />
      <span><strong>新建刷流任务</strong><small>按四步向导完成设置</small></span>
    </button>
  </section>
</template>

<style scoped>
/* 多任务自适应（9.6.1/9.6.3）：3 列封顶 + 末行补位——任意任务数量都不产生孤卡空缺，
   1~2 个任务维持整宽均分，6/8+ 任务整齐分排，纯 CSS 不动组件结构。
   9.6.3 修复：总数恰为 2 时首卡同时命中"倒数第二张"导致 2/3+1/3 误占宽，排除之。 */
.task-health-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(max(300px,30%),1fr));gap:14px}
.task-health-card:last-child:nth-child(3n+1){grid-column:1/-1}
.task-health-card:nth-last-child(2):nth-child(3n+1):not(:nth-child(1)){grid-column:span 2}
.task-health-card{display:flex;flex-direction:column;gap:10px;min-width:0;padding:16px;border:1px solid rgba(var(--v-border-color),var(--v-border-opacity));border-radius:16px;background:rgba(var(--v-theme-surface),.72);color:inherit;text-align:start;cursor:pointer;transition:.18s ease}.task-health-card:hover,.task-health-card.selected{border-color:rgb(var(--v-theme-primary));transform:translateY(-1px)}.task-health-card__top,.task-health-card__capacity,.task-health-card__meta{display:flex;justify-content:space-between;gap:10px}.task-health-card__top>div{display:flex;flex-direction:column}.task-health-card span,.task-health-card small,.task-health-card p{color:rgba(var(--v-theme-on-surface),var(--v-medium-emphasis-opacity));font-size:.8rem}.task-health-card p{margin:0;line-height:1.45}.task-health-card__capacity span{font-size:1.2rem;font-weight:700;color:inherit}.task-health-card--create{align-items:center;align-self:start;flex-direction:row;justify-content:center;min-height:88px;border-style:dashed;text-align:left;color:rgb(var(--v-theme-primary))}.task-health-card--create{grid-column:1/-1}.task-health-card--create>span{display:flex;flex-direction:column;gap:2px}.task-health-card--create strong{font-size:.95rem}.task-health-card--create small{font-size:.76rem}@media(max-width:599px){.task-health-grid{grid-template-columns:1fr}}

/* 紧凑列表（9.6.2）：一行一任务，横向铺开原子载荷；名称/站点/容量/徽章/指标定宽，
   结论占剩余宽度单行截断（悬停显全文），移动端换行堆叠。 */
.task-health-rows{display:flex;flex-direction:column;gap:8px}
.task-health-row{display:flex;align-items:center;gap:12px;min-width:0;padding:10px 14px;border:1px solid rgba(var(--v-border-color),var(--v-border-opacity));border-radius:12px;background:rgba(var(--v-theme-surface),.72);color:inherit;text-align:start;cursor:pointer;transition:.18s ease}
.task-health-row:hover,.task-health-row.selected{border-color:rgb(var(--v-theme-primary));transform:translateY(-1px)}
.task-health-row__name{font-size:.9rem;color:inherit;flex-shrink:0}
.task-health-row__site{color:rgba(var(--v-theme-on-surface),var(--v-medium-emphasis-opacity));font-size:.78rem;flex-shrink:0;white-space:nowrap}
.task-health-row__cap{display:flex;align-items:center;gap:8px;flex:0 1 220px;min-width:130px}
.task-health-row__bar{flex:1}
.task-health-row__cap small{color:rgba(var(--v-theme-on-surface),var(--v-medium-emphasis-opacity));font-size:.75rem;white-space:nowrap}
.task-health-row__meta{color:rgba(var(--v-theme-on-surface),var(--v-medium-emphasis-opacity));font-size:.78rem;flex-shrink:0;white-space:nowrap}
.task-health-row__msg{flex:1;min-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:rgba(var(--v-theme-on-surface),var(--v-medium-emphasis-opacity));font-size:.78rem}
@media(max-width:599px){.task-health-row{flex-wrap:wrap}.task-health-row__msg{flex-basis:100%}}
</style>
