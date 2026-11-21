# 主数据清洗与黄金记录平台

面向多源主数据的匹配识别、冲突消解、合并清洗与黄金记录管理的主数据治理平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 来源系统注册 | `source_system` | 来源系统 | 系统编号、系统名称、系统类型 |
| 实体模型定义 | `entity_model` | 实体模型 | 模型编号、模型名称、业务域 |
| 匹配规则引擎 | `match_rule` | 匹配规则 | 规则编号、实体模型、匹配字段 |
| 疑似重复簇 | `dup_cluster` | 重复簇 | 簇编号、实体模型、簇大小 |
| 合并追溯日志 | `merge_log` | 合并记录 | 记录编号、黄金ID、来源记录 |
| 黄金记录管理 | `golden_record` | 黄金记录 | 黄金ID、实体模型、关联来源 |
| 字段标准化清洗 | `field_cleanse` | 清洗规则 | 规则编号、实体模型、目标字段 |
| 优胜策略配置 | `survivorship_rule` | 优胜策略 | 策略编号、实体模型、选择字段 |
| 数据入库规则 | `data_import_rule` | 入库规则 | 规则编号、来源系统、实体模型 |
| 批次导入管理 | `import_batch` | 导入批次 | 批次编号、来源系统、实体模型 |
| 记录级血缘 | `record_lineage` | 血缘记录 | 血缘编号、黄金ID、来源记录 |
| 主数据分发同步 | `mdm_sync` | 同步配置 | 配置编号、黄金实体、目标系统 |
| 黄金记录版本 | `mdm_version` | 版本记录 | 版本编号、黄金ID、版本号 |
| 自动匹配任务 | `auto_match_job` | 匹配任务 | 任务编号、实体模型、匹配规则 |
| 主数据质量统计 | `mdm_quality` | 质量统计 | 统计编号、实体模型、完备率 |
| 参考数据管理 | `mdm_reference` | 参考数据 | 数据编号、数据类别、标准编码 |
| 主数据查询接口 | `mdm_api` | 查询接口 | 接口编号、实体模型、查询字段 |
| 主数据治理看板 | `mdm_dashboard` | 看板配置 | 看板编号、看板名称、监控实体 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 目标分发对账

`mdm_sync`（主数据分发同步）额外提供分发对账能力，核心实现见
`backend/app/services/distribution_reconciliation.py`，前端在同步模块页面的
「目标分发对账」面板操作。

- **生成批次**（`POST /api/mdm_sync/distribution-batches?sync_id=`）：
  按同步配置的`字段范围`从黄金记录导出完整数据，冻结为一份批次快照（列定义、
  列定义指纹、数据快照、分片范围、合计）。`黄金ID`始终作为首列。
- **同批标识**：批次编号形如 `DIST-<配置编号>-<序号>`。字段范围和数据都没变时
  重发沿用原批次编号；任一口径变化则生成新批次，并把旧批次标记为`已作废`
  且记录`替代批次`，防止两套口径被当成同一批签收。
- **分片导出**（`GET .../distribution-batches/{批次编号}/export?shard_no=`）：
  CSV 文件头带批次编号、列定义、列定义指纹、分片范围（起止行/片数/行数）与
  批次合计，每行带分片定位列，客服可据此比对实收行和缺失片。
- **实收登记与核对**（`POST .../distribution-batches/{批次编号}/receipts`）：
  按`批次编号 + 分片序号`登记实收，可带`golden_ids`逐行明细；返回应收分片、
  实收分片、缺失分片、缺失/多出/重复黄金ID 与本次可确认的批次编号。
- **整体核对**（`POST /api/mdm_sync/{entry_id}/distribution-reconcile`）：
  续发后按同步配置整体判断前后片能否一起签收。
- **批次自检**（`GET .../distribution-batches/{批次编号}/verify`）：复核列定义
  指纹、数据指纹、分片范围连续性与行数合计。
- **口径更正**（`PATCH /api/mdm_sync/{entry_id}/distribution-scope`）：只改同步
  配置本身，不会静默改写已发批次；下一次生成时按新口径换批。

核对结论取值：`同批可签收`、`缺片待补`、`缺行待补`、`实收明细不符`、
`跨批不可合并签收`、`批次已作废`、`批次不存在`。只有`同批可签收`返回
`ok=true` 且 `确认批次编号` 为唯一批次号；跨批次片段一律不合并签收。

测试：`backend/.venv/bin/python -m app.tests.test_distribution_reconciliation`。

领域：数据工程
子方向：数仓与数据湖
