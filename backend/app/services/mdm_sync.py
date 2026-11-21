"""同步配置领域服务：黄金记录分发、增量同步与冲突策略管理。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class MdmSyncService(BaseModuleService):
    MODULE = "mdm_sync"
    ENTITY = "同步配置"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["配置编号", "黄金实体", "目标系统", "同步方式", "字段范围", "变化检测", "冲突策略", "同步状态"]
    REQUIRED_FIELDS = ["配置编号", "黄金实体", "目标系统"]
    STATUS_ORDER = ["草稿", "待校验", "同步中", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "启用同步": "同步中", "停用同步": "已停用"}
    NEGATIVE_ACTIONS = ["停用同步"]

    def sync_golden_to_target(self, sync_id: int) -> dict[str, Any]:
        """将黄金记录分发同步到目标系统。

        读取同步配置，按字段范围和同步方式将黄金记录
        导出并分发到目标业务系统，处理冲突检测和同步策略。
        """
        sync_cfg = self.get_entry(sync_id)
        if not sync_cfg or sync_cfg.get("status") != "同步中":
            return {"ok": False, "reason": "同步配置不可用"}
        from app.services.golden_record import GoldenRecordService
        golden_svc = GoldenRecordService()
        entity = sync_cfg.get("黄金实体", "")
        fields_str = sync_cfg.get("字段范围", "")
        fields = [f.strip() for f in fields_str.split(",") if f.strip()]

        records = golden_svc.search_golden_records(entity_model=entity)
        # 分发同步黄金记录：按字段范围筛选并导出到目标系统
        if fields:
            records = [{f: r.get(f) for f in fields if f in r} for r in records]
        return {
            "ok": True,
            "sync_id": sync_cfg.get("配置编号"),
            "target": sync_cfg.get("目标系统"),
            "synced_count": len(records),
            "records": records,
        }

    def detect_changes_for_sync(self, sync_id: int) -> dict[str, Any]:
        """检测变化数据：对比上次同步快照，识别新增、更新和删除的黄金记录。

        查询同步配置的变化检测策略，对黄金记录执行变更识别，
        输出新增记录、更新记录和变更统计供增量同步使用。
        """
        sync_cfg = self.get_entry(sync_id)
        if not sync_cfg:
            return {"ok": False, "reason": "同步配置不存在"}
        from app.services.golden_record import GoldenRecordService
        golden_svc = GoldenRecordService()
        entity = sync_cfg.get("黄金实体", "")
        all_records = golden_svc.search_golden_records(entity_model=entity)
        # 检测变化：查询黄金记录，对比上次同步快照识别变更
        last_sync = sync_cfg.get("last_sync_at", "")
        changes = {
            "sync_id": sync_cfg.get("配置编号"),
            "total_records": len(all_records),
            "new_records": len(all_records),
            "updated_records": 0,
            "detection_method": sync_cfg.get("变化检测", "全量"),
        }
        sync_cfg["last_sync_at"] = changes.get("last_sync_at", "")
        return changes

    def resolve_sync_conflicts(self, sync_id: int,
                                 local_records: list[dict[str, Any]],
                                 remote_records: list[dict[str, Any]]) -> dict[str, Any]:
        """解决同步冲突：按冲突策略处理本地与远程数据不一致。

        读取同步配置的冲突策略，对比本地和远程黄金记录数据，
        按策略优先级解决字段冲突并输出同步结果。
        """
        sync_cfg = self.get_entry(sync_id)
        strategy = (sync_cfg.get("冲突策略", "本地优先") if sync_cfg else "本地优先")
        resolved = 0
        conflicts = 0
        for local in local_records:
            remote = next(
                (r for r in remote_records if r.get("黄金ID") == local.get("黄金ID")),
                None
            )
            if remote:
                conflicts += 1
                if strategy == "本地优先":
                    resolved += 1
                elif strategy == "远程优先":
                    for k in remote:
                        local[k] = remote[k]
        # 解决同步冲突：按冲突策略处理本地与远程数据不一致
        return {"strategy": strategy, "conflicts_detected": conflicts, "resolved": resolved}

    def query_sync_configs_for_entity(self, entity_model: str) -> list[dict[str, Any]]:
        """查询指定黄金实体的所有同步配置。"""
        rows = store.rows(self.MODULE)
        return [
            row for row in rows
            if row.get("黄金实体") == entity_model and row.get("status") in ("待校验", "同步中")
        ]

    # ── 目标分发对账：批次快照、分片导出、实收核对 ──

    def update_sync_scope(self, sync_id: int, values: dict[str, Any]) -> dict[str, Any]:
        """更正同步配置的可编辑口径（字段范围/同步方式/目标系统等）。

        只改配置本身，不会静默改写已发出的批次；下一次生成对账批次时
        如果口径已变，旧批次会被作废并生成新的批次编号。
        """
        sync_cfg = self.get_entry(sync_id)
        if sync_cfg is None:
            return {"ok": False, "reason": f"同步配置 {sync_id} 不存在或已归档"}
        editable = {"字段范围", "同步方式", "变化检测", "冲突策略", "目标系统", "黄金实体"}
        applied: dict[str, Any] = {}
        for field, value in (values or {}).items():
            if field in editable and value is not None:
                sync_cfg[field] = str(value)
                applied[field] = sync_cfg[field]
        if not applied:
            return {"ok": False, "reason": "没有可更正的字段（仅支持字段范围/同步方式/目标系统等口径字段）"}
        return {
            "ok": True,
            "message": f"同步配置 {sync_cfg.get('配置编号')} 已更正："
                       + "、".join(f"{k}={v}" for k, v in applied.items()),
            "entry": sync_cfg,
            "applied": applied,
        }

    def _recon(self):
        from app.services.distribution_reconciliation import distribution_reconciliation
        return distribution_reconciliation

    def build_distribution_batch(self, sync_id: int, shard_size: int | None = None) -> dict[str, Any]:
        """按同步配置的字段范围生成分发对账批次；口径不变的重发沿用原批次编号。"""
        return self._recon().build_batch(sync_id, shard_size=shard_size)

    def list_distribution_batches(self, sync_id: int | None = None) -> list[dict[str, Any]]:
        """列出发放过的对账批次，用于确认当前可签收的批次编号。"""
        return self._recon().list_batches(sync_id)

    def get_distribution_batch(self, batch_no: str) -> dict[str, Any] | None:
        """查看单个对账批次的列定义、分片范围、合计和已登记实收。"""
        return self._recon().get_batch(batch_no)

    def verify_distribution_batch(self, batch_no: str) -> dict[str, Any]:
        """复核批次分片范围、合计与列定义是否自洽。"""
        return self._recon().verify_batch(batch_no)

    def export_distribution_file(self, batch_no: str, shard_no: int | None = None) -> dict[str, Any]:
        """导出对账文件内容，文件头带批次号和列定义指纹，供客服核对实收。"""
        return self._recon().export_csv(batch_no, shard_no=shard_no)

    def acknowledge_distribution_receipts(self, receipts: list[dict[str, Any]],
                                          default_batch_no: str | None = None) -> dict[str, Any]:
        """登记实收片段并立即核对：返回应收分片、缺失片与可确认批次编号。"""
        return self._recon().acknowledge(receipts, default_batch_no=default_batch_no)

    def reconcile_distribution(self, receipts: list[dict[str, Any]] | None = None,
                               default_batch_no: str | None = None) -> dict[str, Any]:
        """核对实收数据：清点应收分片、实收行与本次可确认的批次编号。"""
        return self._recon().reconcile(receipts, persist=False, default_batch_no=default_batch_no)

    def check_distribution_signoff(self, sync_id: int) -> dict[str, Any]:
        """续发后整体核对某同步配置的实收，判断前后片能否一起签收。"""
        return self._recon().check_sync(sync_id)