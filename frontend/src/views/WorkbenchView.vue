<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <section v-if="isDistributionModule" class="recon-panel" data-testid="distribution-panel">
      <header class="recon-head">
        <h3>目标分发对账</h3>
        <p class="page-desc">
          按同批标识清点分片：生成批次 → 按片导出 → 登记实收 → 核对缺失片与可签收批次。
          同口径重发沿用原批次编号；字段范围变化会作废旧批次并要求整批重收。
        </p>
      </header>

      <form class="recon-form" @submit.prevent="buildBatch">
        <label>
          <span>同步配置</span>
          <select v-model.number="reconSyncId" data-testid="recon-sync" @change="selectSync">
            <option v-for="row in rows" :key="String(row.id)" :value="Number(row.id)">
              {{ row['配置编号'] }} · {{ row['黄金实体'] }} → {{ row['目标系统'] }}（{{ row.status }}）
            </option>
          </select>
        </label>
        <label>
          <span>分片大小</span>
          <input v-model.number="shardSize" type="number" min="1" data-testid="recon-shard-size" />
        </label>
        <button class="btn primary" type="submit" data-testid="recon-build" :disabled="reconBusy">
          生成 / 续发批次
        </button>
        <button class="btn" type="button" data-testid="recon-signoff" :disabled="reconBusy || !reconSyncId" @click="checkSignoff">
          整体核对签收
        </button>
        <span v-if="reconMessage" class="recon-message" data-testid="recon-message">{{ reconMessage }}</span>
      </form>

      <form class="recon-form" @submit.prevent="saveScope">
        <label class="recon-scope">
          <span>更正同步字段范围（逗号分隔，变更后续发会生成新批次并作废旧批次）</span>
          <input v-model="scopeDraft" data-testid="recon-scope" placeholder="例如 name,phone,level,region" />
        </label>
        <button class="btn" type="submit" data-testid="recon-save-scope" :disabled="reconBusy || !reconSyncId">
          保存字段配置
        </button>
      </form>

      <p v-if="reconError" class="error-text" data-testid="recon-error">{{ reconError }}</p>

      <div v-if="batches.length" class="recon-batches">
        <article
          v-for="batch in batches"
          :key="batch.批次编号"
          class="recon-batch"
          :class="{ superseded: batch.状态 === '已作废' }"
          :data-testid="`batch-${batch.批次编号}`"
        >
          <div class="recon-batch-head">
            <strong>{{ batch.批次编号 }}</strong>
            <span class="badge" :class="`outcome-${batch.状态}`">{{ batch.状态 }}</span>
            <span class="muted">列指纹 {{ batch.列定义指纹 }}</span>
            <span class="muted">合计 {{ batch.总行数 }} 行 / {{ batch.分片总数 }} 片</span>
            <span class="muted">已登记 {{ batch.已登记分片数 }} / {{ batch.分片总数 }} 片</span>
            <button class="link" type="button" @click="downloadBatch(batch.批次编号)">
              导出整批
            </button>
          </div>
          <p v-if="batch.替代批次" class="recon-note">
            已在 {{ batch.生成时间 }} 被 {{ batch.替代批次 }} 替代，旧片段不能与新批次一起签收。
          </p>
          <p class="muted">列定义：{{ batch.列定义.join(' | ') }}</p>
          <table class="data-table recon-shards">
            <thead>
              <tr>
                <th>分片编号</th><th>行范围</th><th>行数</th><th>黄金ID范围</th>
                <th>导出</th><th>登记实收</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="shard in batch.分片" :key="shard.分片编号">
                <td>{{ shard.分片编号 }}</td>
                <td>{{ shard.起始行 }}-{{ shard.结束行 }}</td>
                <td>{{ shard.行数 }}</td>
                <td class="muted">
                  {{ shard.黄金ID范围[0] }}<template v-if="shard.黄金ID范围.length > 1"> … {{ shard.黄金ID范围[shard.黄金ID范围.length - 1] }}</template>
                </td>
                <td>
                  <button class="link" type="button" @click="downloadBatch(batch.批次编号, shard.分片序号)">
                    下载片段
                  </button>
                </td>
                <td>
                  <button
                    class="link"
                    type="button"
                    :disabled="batch.状态 === '已作废'"
                    :data-testid="`ack-${shard.分片编号}`"
                    @click="acknowledge(batch, shard.分片序号)"
                  >
                    {{ receiptLabel(batch, shard.分片序号) }}
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </article>
      </div>

      <div v-if="reconResult" class="recon-result" data-testid="recon-result">
        <div class="recon-result-head">
          <span class="badge" :class="`outcome-${reconResult.outcome}`">{{ reconResult.outcome }}</span>
          <span data-testid="recon-confirmable">
            本次可确认批次：{{ reconResult.可确认批次编号.length ? reconResult.可确认批次编号.join('、') : '无' }}
          </span>
          <span class="muted">应收 {{ reconResult.合计.应收行数 }} 行 / 实收 {{ reconResult.合计.实收行数 }} 行</span>
        </div>
        <p>{{ reconResult.message }}</p>
        <p v-if="reconResult.缺失分片.length" data-testid="recon-missing-shards">
          缺失分片：{{ reconResult.缺失分片.map((item) => item.分片编号).join('、') }}
        </p>
        <p v-if="reconResult.缺失黄金ID.length" class="muted">
          缺失行：{{ reconResult.缺失黄金ID.slice(0, 8).join('、') }}<template v-if="reconResult.缺失黄金ID.length > 8"> 等 {{ reconResult.缺失黄金ID.length }} 行</template>
        </p>
        <p v-if="reconResult.多出黄金ID.length" class="error-text">
          多出行：{{ reconResult.多出黄金ID.join('、') }}
        </p>
        <table v-if="Object.keys(reconResult.批次核对).length" class="data-table recon-batch-table">
          <thead>
            <tr><th>批次编号</th><th>批次状态</th><th>列指纹</th><th>应收/实收行</th><th>结论</th></tr>
          </thead>
          <tbody>
            <tr v-for="(report, batchNo) in reconResult.批次核对" :key="batchNo">
              <td>{{ batchNo }}</td>
              <td>{{ report.批次状态 }}</td>
              <td>{{ report.列定义指纹 }}</td>
              <td>{{ report.应收行数 }} / {{ report.实收行数 }}</td>
              <td>{{ report.完整 ? '完整' : '未收齐' }}<template v-if="report.批次状态 === '已作废'">（不可签收）</template></td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import {
  acknowledgeDistributionReceipts,
  buildDistributionBatch,
  createEntry,
  downloadDistributionBatch,
  downloadEntries,
  getDistributionBatch,
  listDistributionBatches,
  listEntries,
  moduleMeta,
  reconcileSyncDistribution,
  runAction as applyAction,
  updateDistributionScope,
} from '@/api/backend-service'
import type {
  DistributionBatch,
  DistributionReconcileResult,
  EntryRow,
} from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

// ── 目标分发对账（仅主数据分发同步模块）──
const isDistributionModule = computed(() => meta.key === 'mdm_sync')
const reconSyncId = ref<number | null>(null)
const shardSize = ref(500)
const batches = ref<DistributionBatch[]>([])
const reconResult = ref<DistributionReconcileResult | null>(null)
const reconMessage = ref('')
const reconError = ref('')
const reconBusy = ref(false)
const acknowledged = ref<Record<string, number[]>>({})
const scopeDraft = ref('')

function currentSyncRow(): EntryRow | undefined {
  return rows.value.find((row) => Number(row.id) === reconSyncId.value)
}

function syncScopeDraft() {
  scopeDraft.value = String(currentSyncRow()?.['字段范围'] ?? '')
}

async function saveScope() {
  if (!reconSyncId.value) return
  reconBusy.value = true
  reconError.value = ''
  try {
    const result = await updateDistributionScope(reconSyncId.value, scopeDraft.value)
    if (!result.ok) throw new Error(result.message)
    reconMessage.value = result.message
    await reload()
  } catch (error) {
    reconError.value = error instanceof Error ? error.message : '同步字段范围保存失败'
  } finally {
    reconBusy.value = false
  }
}

function resetReconState() {
  batches.value = []
  reconResult.value = null
  reconMessage.value = ''
  reconError.value = ''
  acknowledged.value = {}
}

async function loadBatches() {
  if (!isDistributionModule.value || !reconSyncId.value) return
  reconError.value = ''
  try {
    batches.value = await listDistributionBatches(reconSyncId.value)
    const acked: Record<string, number[]> = {}
    for (const batch of batches.value) {
      if (!batch.实收?.length) continue
      const detail = await getDistributionBatch(batch.批次编号)
      acked[batch.批次编号] = detail.实收.map((item) => item.分片序号)
    }
    acknowledged.value = acked
  } catch (error) {
    reconError.value = error instanceof Error ? error.message : '分发对账批次读取失败'
  }
}

async function buildBatch() {
  if (!reconSyncId.value) return
  reconBusy.value = true
  reconError.value = ''
  try {
    const result = await buildDistributionBatch(reconSyncId.value, {
      shardSize: shardSize.value || undefined,
    })
    if (!result.ok) throw new Error(result.message)
    reconMessage.value = result.message
    await loadBatches()
  } catch (error) {
    reconError.value = error instanceof Error ? error.message : '分发对账批次生成失败'
  } finally {
    reconBusy.value = false
  }
}

async function downloadBatch(batchNo: string, shardNo?: number) {
  reconError.value = ''
  try {
    await downloadDistributionBatch(batchNo, shardNo)
  } catch (error) {
    reconError.value = error instanceof Error ? error.message : '分发对账文件导出失败'
  }
}

function receiptLabel(batch: DistributionBatch, shardNo: number): string {
  return acknowledged.value[batch.批次编号]?.includes(shardNo) ? '已登记，重新核对' : '登记实收'
}

async function acknowledge(batch: DistributionBatch, shardNo: number) {
  reconBusy.value = true
  reconError.value = ''
  try {
    const result = await acknowledgeDistributionReceipts(batch.批次编号, [
      { batch_no: batch.批次编号, shard_no: shardNo },
    ])
    reconResult.value = result
    reconMessage.value = result.message
    await loadBatches()
  } catch (error) {
    reconError.value = error instanceof Error ? error.message : '实收登记失败'
  } finally {
    reconBusy.value = false
  }
}

async function checkSignoff() {
  if (!reconSyncId.value) return
  reconBusy.value = true
  reconError.value = ''
  try {
    const result = await reconcileSyncDistribution(reconSyncId.value)
    reconResult.value = result
    reconMessage.value = result.message
    await loadBatches()
  } catch (error) {
    reconError.value = error instanceof Error ? error.message : '整体核对失败'
  } finally {
    reconBusy.value = false
  }
}

function syncReconConfig() {
  if (!isDistributionModule.value || !rows.value.length) return
  if (reconSyncId.value && rows.value.some((row) => Number(row.id) === reconSyncId.value)) return
  reconSyncId.value = Number(rows.value[0].id)
  syncScopeDraft()
  void loadBatches()
}

function selectSync() {
  reconResult.value = null
  reconMessage.value = ''
  syncScopeDraft()
  void loadBatches()
}

watch(rows, syncReconConfig)
watch(isDistributionModule, (active) => {
  if (active) {
    syncReconConfig()
  } else {
    resetReconState()
  }
}, { immediate: true })

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>
