/** 纯前端数据层的公共类型：与全栈版后端返回的结构保持一致，换回后端时页面不用改。 */

export type EntryRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  [field: string]: string | number | boolean
}

export type ModuleMeta = {
  key: string
  name: string
  entity: string
  desc: string
  fields: string[]
  statuses: string[]
  actions: string[]
  actionTargets: Record<string, string>
  metrics: string[]
}

export type PageResult = {
  items: EntryRow[]
  total: number
  page: number
  size: number
}

export type ActionResult = {
  ok: boolean
  message: string
}

export type OverviewResult = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}

/** 分发对账批次：一次实际分发冻结下来的列定义、分片范围与合计。 */
export type DistributionShard = {
  分片编号: string
  分片序号: number
  起始行: number
  结束行: number
  行数: number
  黄金ID范围: string[]
  摘要: string
  实收行数?: number
  明细完整?: boolean
  缺失黄金ID?: string[]
  多出黄金ID?: string[]
  状态?: string
}

export type DistributionBatch = {
  批次编号: string
  配置编号: string
  目标系统: string
  黄金实体: string
  列定义: string[]
  列定义指纹: string
  字段范围原文: string
  总行数: number
  分片大小: number
  分片总数: number
  已登记分片数: number
  生成时间: string
  状态: string
  替代批次: string
  重发来源: string
  分片: DistributionShard[]
  实收: { 分片序号: number; 行数: number; 黄金ID: string[] }[]
  自检: {
    ok: boolean
    checks: Record<string, boolean>
    总行数: number
    分片总数: number
    列定义: string[]
  }
}

export type DistributionBuildResult = {
  ok: boolean
  resend: boolean
  created: boolean
  message: string
  superseded: { batch_no: string; reason: string } | null
  batch: DistributionBatch
}

export type DistributionReconcileResult = {
  ok: boolean
  outcome: string
  message: string
  确认批次编号: string | null
  可确认批次编号: string[]
  不可确认批次编号: string[]
  列定义指纹: string[]
  列定义一致: boolean
  应收分片: DistributionShard[]
  实收分片: DistributionShard[]
  缺失分片: DistributionShard[]
  合计: { 应收行数: number; 实收行数: number; 缺失行数: number; 多出行数: number }
  缺失黄金ID: string[]
  多出黄金ID: string[]
  重复黄金ID: string[]
  批次核对: Record<string, {
    批次编号: string
    批次状态: string
    列定义: string[]
    列定义指纹: string
    替代批次: string
    完整: boolean
    应收行数: number
    实收行数: number
    缺失黄金ID: string[]
    多出黄金ID: string[]
    重复黄金ID: string[]
  }>
}
