# 文化企业融资进度穿透

本项目整理文化企业多方融资（银行贷款、财政补贴、社会投资）进度穿透领域的事件约定与穿透式进度账本实现，把申请、重叠提示、里程碑验收、分批放款和追偿事件扩展为可复算、可追溯的资金账本。资料只包含领域约定与虚构样例，不包含真实个人信息、生产连接或外部账号。

## 领域事件

| 事件 | 含义 |
| --- | --- |
| `CONTRACT_COMMITTED` | 资金合同登记：锁定用途、成本项、出资比例与生效条件 |
| `APPLICATION_SUBMITTED` | 验收申请提交（含成本项、金额、票据号） |
| `OVERLAP_FLAGGED` | 重复覆盖提示（跨资金来源 / 跨申请），仅提示不处置 |
| `OVERLAP_RESOLVED` | 独立审核人决定：拆分（SPLIT）/ 拒绝（REJECT）/ 驳回（DISMISS） |
| `MILESTONE_ACCEPTED` | 里程碑验收，支持部分验收与版本化调整（后补票据、撤销结论） |
| `TRANCHE_RELEASED` | 分批放款，金额由账本按当前验收版本计算 |
| `RECOVERY_RECONCILED` | 追偿核销，超付金额收回合同可用额度 |

## 穿透式进度账本（`src/ledger.py`）

`ProgressLedger` 以事件溯源方式实现，所有状态由事件日志推导：

- **资金锁定**：`commit_contract` 登记每份合同的用途、成本项、出资比例（0,1]、单项额度与生效条件（`milestone:<id>`），承诺金额不得超过各成本项额度合计。
- **重叠提示与独立审核**：`submit_application` 自动识别跨资金来源（出资比例合计 > 1）与跨申请（同一成本重复申报）的重复覆盖，生成 `OVERLAP_FLAGGED` 并阻塞验收；自动结果只是提示，必须由独立审核人（非申请人、非相关出资方）通过 `resolve_overlap` 决定拆分比例或拒绝。
- **部分验收与版本化调整**：`accept_milestone` 的 `accepted_ratio` 控制部分验收，只释放对应比例；后补票据与撤销结论以新版本（`adjusted_claims` 完整重述 + `reason`）调整，原放款事件不被改写，差额通过追加放款或追偿核销。
- **分批放款**：`release_tranche` 放款额 = 当前版本覆盖额 − 该申请链已放额；校验生效条件、合同可用额度与单项额度，幂等键去重，并发调用在锁内串行，不会多付。
- **追偿与金额守恒**：`reconcile_recovery` 只允许多追到「已放 − 当前覆盖」的余额；`verify_conservation` 校验每份合同 `committed == available + released − recovered`、净放款不超承诺、同一成本项覆盖不超过验收额。
- **可见性**：`funder_view` 只给出资方自身合同、自身放款/追偿与脱敏重叠摘要（不含其他出资方身份与金额）；`project_view` 给项目方全链路；`regulator_view` 提供完整日志、各合同头寸，并通过 `audit` 重放日志复算放款/追偿金额。
- **幂等与恢复**：所有命令按 `event_id` 去重（重复回执返回 `DUPLICATE`），放款另有幂等键；`replay` 从持久化日志恢复，重复事件自动去重，恢复后可继续交易。
- **追溯**：`trace_release` 从任一到账金额反查成本构成、验收版本链与审批链（重叠提示 + 审核决定）。

## 快速开始

```python
from src.ledger import ProgressLedger

ledger = ProgressLedger()
ledger.commit_contract(event_id="e-1", occurred_at="2026-09-25T09:00:00+08:00",
                       contract_id="CT-BANK", project_id="PRJ-1",
                       funder_id="BANK-A", funder_kind="BANK_LOAN", purpose="设备购置",
                       cost_items={"EQUIP": {"ratio": "0.6", "cap": "600000"}},
                       conditions=[], committed_amount="600000")
# …登记其余合同…
res = ledger.submit_application(event_id="e-2", occurred_at="2026-09-25T10:00:00+08:00",
                                application_id="APP-1", project_id="PRJ-1",
                                milestone_id="M2", applicant_id="PM-1",
                                claims=[{"cost_item_id": "EQUIP", "amount": "1000000",
                                         "invoice_refs": ["INV-001"]}])
# res["flags"] 即重复覆盖提示；审核人拆分后验收、放款
```

完整链路（重叠拆分、部分验收、后补票据、撤销追偿、视图与追溯）见 `tests/test_ledger.py`。

## 目录

- `src/culture_finance_progress.py`：事件种类与最小字段校验。
- `src/ledger.py`：穿透式进度账本。
- `data/sample.json`：用于核对资料格式的虚构事件。
- `tests/`：领域约定与账本行为测试。

## 本地核对

测试命令：

```bash
python3 -m unittest discover -s tests
```

编译或构建命令：

```bash
python3 -m compileall -q .
```

所有测试和构建均在单个 Linux 应用容器内完成，不需要另行启动数据库或外部服务。
