import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type {
  ActionResult,
  DistributionBatch,
  DistributionBuildResult,
  DistributionReconcileResult,
  EntryRow,
  OverviewResult,
  PageResult,
} from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ── 目标分发对账：批次生成、分片导出、实收登记与签收核对 ──

export async function buildDistributionBatch(
  syncId: number,
  options: { shardSize?: number } = {},
): Promise<DistributionBuildResult> {
  const response = await request(`/api/mdm_sync/distribution-batches?sync_id=${syncId}`, {
    method: 'POST',
    body: JSON.stringify({ shard_size: options.shardSize ?? null }),
  })
  if (!response.ok) throw new Error('分发对账批次生成失败')
  return await response.json()
}

export async function listDistributionBatches(syncId: number): Promise<DistributionBatch[]> {
  const response = await request(`/api/mdm_sync/distribution-batches?sync_id=${syncId}`)
  if (!response.ok) throw new Error('分发对账批次读取失败')
  return await response.json()
}

export async function getDistributionBatch(batchNo: string): Promise<DistributionBatch> {
  const response = await request(`/api/mdm_sync/distribution-batches/${encodeURIComponent(batchNo)}`)
  if (!response.ok) throw new Error(`分发对账批次 ${batchNo} 读取失败`)
  return await response.json()
}

export async function downloadDistributionBatch(
  batchNo: string,
  shardNo?: number,
): Promise<void> {
  const query = shardNo === undefined ? '' : `?shard_no=${shardNo}`
  const response = await request(
    `/api/mdm_sync/distribution-batches/${encodeURIComponent(batchNo)}/export${query}`,
  )
  if (!response.ok) throw new Error('分发对账文件导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = shardNo === undefined
    ? `${batchNo}.csv`
    : `${batchNo}-S${String(shardNo).padStart(2, '0')}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export type ReceiptInput = {
  batch_no: string
  shard_no: number
  row_count?: number | null
  golden_ids?: string[] | null
}

export async function acknowledgeDistributionReceipts(
  batchNo: string,
  receipts: ReceiptInput[],
): Promise<DistributionReconcileResult> {
  const response = await request(
    `/api/mdm_sync/distribution-batches/${encodeURIComponent(batchNo)}/receipts`,
    { method: 'POST', body: JSON.stringify({ default_batch_no: batchNo, receipts }) },
  )
  if (!response.ok) throw new Error('实收登记失败')
  return await response.json()
}

export async function reconcileSyncDistribution(
  syncId: number,
  receipts: ReceiptInput[] = [],
): Promise<DistributionReconcileResult> {
  const response = await request(`/api/mdm_sync/${syncId}/distribution-reconcile`, {
    method: 'POST',
    body: JSON.stringify({ receipts }),
  })
  if (!response.ok) throw new Error('分发对账核对失败')
  return await response.json()
}

export async function updateDistributionScope(
  syncId: number,
  fieldRange: string,
): Promise<ActionResult> {
  const response = await request(`/api/mdm_sync/${syncId}/distribution-scope`, {
    method: 'PATCH',
    body: JSON.stringify({ values: { 字段范围: fieldRange } }),
  })
  if (!response.ok) throw new Error('同步字段范围保存失败')
  return await response.json()
}
