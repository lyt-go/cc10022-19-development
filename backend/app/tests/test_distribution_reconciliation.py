"""分发对账链路测试：按同批标识生成、续发、分段登记与签收判定。"""
from __future__ import annotations

import os
import sys

_backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from app.seed import SEED_ROWS
from app.services.mdm_sync import MdmSyncService
from app.services.distribution_reconciliation import BATCH_TABLE
from app.store import store


def _reset_tables() -> None:
    """每个用例从示例数据重建，避免用例之间共享全局内存仓库互相干扰。"""
    for module in ("golden_record", "mdm_sync"):
        store.rows(module).clear()
        store.rows(module).extend(dict(row) for row in SEED_ROWS[module])
    store.rows(BATCH_TABLE).clear()


def _prepare_sync(fields: str = "name,phone", shard_size: int = 2):
    """恢复示例数据，并把 customer 同步配置改成同步中；示例中有 5 条已生效黄金记录。"""
    _reset_tables()
    sync_svc = MdmSyncService()
    cfg = sync_svc.get_entry(201)
    cfg.update({"黄金实体": "customer", "字段范围": fields, "status": "同步中"})
    return sync_svc, cfg


def test_partial_receipt_reports_missing_shards_and_no_confirmable_batch():
    """客服只收了前半批：应报缺失分片，且本次不可确认批次编号。"""
    sync_svc, cfg = _prepare_sync()
    built = sync_svc.build_distribution_batch(201, shard_size=2)
    batch = built["batch"]
    assert batch["分片总数"] == 3, batch

    result = sync_svc.acknowledge_distribution_receipts([
        {"batch_no": batch["批次编号"], "shard_no": 1},
        {"batch_no": batch["批次编号"], "shard_no": 2},
    ], default_batch_no=batch["批次编号"])

    assert result["outcome"] == "缺片待补"
    assert result["确认批次编号"] is None
    assert result["可确认批次编号"] == []
    assert [item["分片序号"] for item in result["缺失分片"]] == [3]
    assert result["合计"]["实收行数"] < result["合计"]["应收行数"]


def test_same_columns_resend_keeps_batch_number():
    """同口径重发不得换批次编号，否则客服无法判断前后片是否同一批。"""
    sync_svc, _ = _prepare_sync()
    first = sync_svc.build_distribution_batch(201, shard_size=2)
    second = sync_svc.build_distribution_batch(201, shard_size=2)
    assert second["created"] is False
    assert second["batch"]["批次编号"] == first["batch"]["批次编号"]


def test_column_change_supersedes_old_batch_and_blocks_cross_signoff():
    """字段范围被更正后：旧批次作废，前后片不能作为同一批签收。"""
    sync_svc, cfg = _prepare_sync(fields="name,phone")
    old = sync_svc.build_distribution_batch(201, shard_size=2)
    old_no = old["batch"]["批次编号"]

    # 收到前半批后，有人更正了同步字段配置
    cfg["字段范围"] = "name,phone,region"
    new = sync_svc.build_distribution_batch(201, shard_size=2)
    new_no = new["batch"]["批次编号"]
    assert new_no != old_no
    assert new["superseded"]["batch_no"] == old_no
    assert sync_svc.get_distribution_batch(old_no)["状态"] == "已作废"

    # 续发后半批：前半来自旧批次、后半来自新批次
    result = sync_svc.reconcile_distribution([
        {"batch_no": old_no, "shard_no": 3},
        {"batch_no": new_no, "shard_no": 2},
    ])
    assert result["outcome"] == "跨批不可合并签收"
    assert result["列定义一致"] is False
    assert result["可确认批次编号"] == []
    assert set(result["批次核对"]) == {old_no, new_no}


def test_full_batch_signoff_confirms_single_batch_number():
    """同一批次收齐后可确认唯一批次编号。"""
    sync_svc, _ = _prepare_sync()
    batch = sync_svc.build_distribution_batch(201, shard_size=2)["batch"]
    result = sync_svc.reconcile_distribution([
        {"batch_no": batch["批次编号"], "shard_no": index}
        for index in range(1, batch["分片总数"] + 1)
    ])
    assert result["outcome"] == "同批可签收"
    assert result["ok"] is True
    assert result["确认批次编号"] == batch["批次编号"]


def test_resend_single_shard_recounts_rows_and_reports_batch_number():
    """重送其中一个文件片段：只按该片段核对行数，并报出所属批次编号。"""
    sync_svc, _ = _prepare_sync()
    batch = sync_svc.build_distribution_batch(201, shard_size=2)["batch"]
    shard = batch["分片"][0]

    # 只重送第一片，且少送了一行
    result = sync_svc.reconcile_distribution([
        {"batch_no": batch["批次编号"], "shard_no": 1,
         "golden_ids": shard["黄金ID范围"][:1]},
    ])
    assert result["确认批次编号"] is None
    assert result["缺失分片"], "其余分片应报告为缺失"
    assert shard["黄金ID范围"][1] in result["缺失黄金ID"]
    report = result["批次核对"][batch["批次编号"]]
    assert report["实收行数"] == 1
    assert report["完整"] is False


def test_export_header_carries_batch_columns_and_shard_range():
    """导出文件要能按一次实际分发复核：批次号、列定义、分片范围、合计都在。"""
    sync_svc, _ = _prepare_sync(fields="name,phone")
    batch = sync_svc.build_distribution_batch(201, shard_size=2)["batch"]
    exported = sync_svc.export_distribution_file(batch["批次编号"], shard_no=2)
    assert exported["ok"] is True
    content = exported["content"]
    assert f"# 批次编号,{batch['批次编号']}" in content
    assert "# 列定义,黄金ID|name|phone" in content
    assert f"# 分片范围,3-4,共1片,共2行" in content
    assert f"# 批次合计,总行数={batch['总行数']},分片总数=3" in content
    data_lines = [line for line in content.splitlines()
                  if line.startswith(batch["批次编号"] + ",")]
    assert len(data_lines) == 2
    header = content.splitlines()[-3]
    assert header.startswith("批次编号,列定义指纹,分片编号")


def test_verify_batch_checks_shard_contiguity_and_totals():
    """批次自检：分片范围连续、行数合计等于总行数。"""
    sync_svc, _ = _prepare_sync()
    batch = sync_svc.build_distribution_batch(201, shard_size=2)["batch"]
    verified = sync_svc.verify_distribution_batch(batch["批次编号"])
    assert verified["ok"] is True
    assert verified["checks"]["分片范围连续无重叠"] is True
    assert verified["checks"]["分片行数合计等于总行数"] is True


def test_signoff_after_scope_correction_blocks_mixed_columns():
    """客服已收前半批，字段配置被更正后续发后半批：整体核对必须判跨批不可签收。

    收齐新批次后，旧批次的半批仍不应被当成可签收的一部分。
    """
    sync_svc, cfg = _prepare_sync(fields="name,phone")
    old_no = sync_svc.build_distribution_batch(201, shard_size=2)["batch"]["批次编号"]
    sync_svc.acknowledge_distribution_receipts([
        {"batch_no": old_no, "shard_no": 1},
        {"batch_no": old_no, "shard_no": 2},
    ], default_batch_no=old_no)

    # 有人更正了同步字段配置，续发生成新批次
    cfg["字段范围"] = "name,phone,region"
    new_no = sync_svc.build_distribution_batch(201, shard_size=2)["batch"]["批次编号"]

    # 新批次还差最后一片：旧批次的半批混进来，必须判跨批不可合并签收
    sync_svc.acknowledge_distribution_receipts([
        {"batch_no": new_no, "shard_no": 1},
        {"batch_no": new_no, "shard_no": 2},
    ], default_batch_no=new_no)
    mixed = sync_svc.check_distribution_signoff(201)
    assert mixed["outcome"] == "跨批不可合并签收", mixed
    assert mixed["列定义一致"] is False
    assert mixed["确认批次编号"] is None

    # 新批次整批收齐后，可以只按新批次签收
    sync_svc.acknowledge_distribution_receipts([
        {"batch_no": new_no, "shard_no": 3},
    ], default_batch_no=new_no)
    signed = sync_svc.check_distribution_signoff(201)
    assert signed["outcome"] == "同批可签收", signed
    assert signed["确认批次编号"] == new_no
    assert old_no in signed["批次核对"]
    assert signed["批次核对"][old_no]["批次状态"] == "已作废"


if __name__ == "__main__":
    tests = [
        ("部分签收报缺失片", test_partial_receipt_reports_missing_shards_and_no_confirmable_batch),
        ("同口径重发沿用批次号", test_same_columns_resend_keeps_batch_number),
        ("字段变更作废旧批次", test_column_change_supersedes_old_batch_and_blocks_cross_signoff),
        ("整批收齐可签收", test_full_batch_signoff_confirms_single_batch_number),
        ("重送单片段核对", test_resend_single_shard_recounts_rows_and_reports_batch_number),
        ("导出文件头完整", test_export_header_carries_batch_columns_and_shard_range),
        ("批次自检", test_verify_batch_checks_shard_contiguity_and_totals),
        ("字段变更后整体签收", test_signoff_after_scope_correction_blocks_mixed_columns),
    ]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS: {name}")
            passed += 1
        except Exception as exc:  # noqa: BLE001 - 测试入口需要打印失败原因
            print(f"  FAIL: {name} -> {exc}")
            failed += 1
    print(f"\n结果: {passed}/{len(tests)} 通过, {failed} 失败")
    assert failed == 0, f"{failed} 条分发对账链路测试失败！"
