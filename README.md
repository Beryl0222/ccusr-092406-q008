# 文化企业融资进度穿透

文化项目同时使用银行贷款、财政补贴和社会投资时，各出资方按各自里程碑拨款，
同一笔成本（例如一次设备验收）可能被重复申报、重复覆盖。本项目把**申请、资金
锁定、重叠提示、里程碑验收、分批放款、追偿**建成一套只追加（append-only）的
**穿透式进度账本**，并给出事件契约、金额守恒不变式与可复算视图。资料只包含
领域约定，不含真实个人信息、生产连接或外部账号。

## 目录

- `src/culture_finance_progress.py`：事件契约、事件折叠（fold）与账本实现。
- `tests/`：契约、守恒不变式、幂等、并发、崩溃恢复、视图脱敏与追溯测试。
- `data/sample.json`：用于核对资料格式的虚构事件。

## 账本模型

所有事实都是不可改写的事件，当前状态由事件序列折叠得到；事件带严格递增的
`seq`，金额一律以两位小数字符串存储，内部用 `Decimal` 运算。

| 事件 | 含义 |
| --- | --- |
| `APPLICATION_SUBMITTED` | 项目立项 / 资金申请 |
| `FUND_LOCKED` | 资金合同锁定：**用途、成本项、出资比例、生效条件** |
| `CONDITION_MET` | 合同生效条件满足（放款闸门） |
| `ACCEPTANCE_SUBMITTED` | 项目方提交里程碑验收（可一次申报多资金来源） |
| `OVERLAP_FLAGGED` | 系统识别跨来源重复覆盖，**仅提示**并给建议拆分 |
| `ACCEPTANCE_REVIEWED` | 独立审核人决定：批准 / 拆分 / 拒绝 |
| `MILESTONE_ACCEPTED` | 验收结论定稿（版本 1），决定各资金可释放额 |
| `ACCEPTANCE_AMENDED` | 新版本：后补票据（SUPPLEMENT）或撤销下调（REVOCATION） |
| `TRANCHE_RELEASED` | 分批放款，按到账回单号幂等 |
| `RECOVERY_RECONCILED` | 追偿到账，冲减欠款并恢复可用额度 |

### 关键规则

1. **自动结果只提示，人做决定**：提交验收时系统识别同次申报多来源、跨验收
   重复、超额覆盖、可用额度不足、重复票据五类情形，写入 `OVERLAP_FLAGGED`
   并按合同比例给出建议拆分；但只有独立审核人（不得是提交人）的
   `ACCEPTANCE_REVIEWED` 能定稿，审核人可以批准、按拆分定稿或拒绝。
2. **红线不可越过**：成本项累计获准覆盖 ≤ 成本金额；单笔资金累计获准 ≤
   承诺额；生效条件未满足不得放款。即便审核人也不能批准突破红线的方案。
3. **部分验收只释放对应比例**：放款额 ≤ 该验收当前版本对该资金的获准余额，
   且 ≤ 资金可用额度，可分多笔放款。
4. **新版本不改写历史**：后补票据 / 撤销结论追加 `ACCEPTANCE_AMENDED`，原
   `TRANCHE_RELEASED` 永不修改。新版本下调导致净放款超过新结论时，超出部分
   记为 `recovery_due`，必须先追偿才能继续对该验收放款。
5. **金额守恒**：对每笔资金
   `承诺额 = 可用额度 + 净放款额（累计放款 − 累计追偿）`，
   追偿不得超过对应净放款；`recompute()` 与监管视图可随时复算全部口径。
6. **故障安全**：
   - 提交类命令携带客户端 `request_id`，按「请求号 + 阶段」幂等；放款 / 追偿
     按银行回单号 `receipt_id` 幂等，重复回执绝不二次放款。
   - 命令带 `expected_seq` 做乐观并发；所有命令在进程内串行化，并发放款不会
     双花。
   - 多事件命令（提交+提示、审核+定稿）支持**断点续跑**：进程在两事件之间
     崩溃，重试时从已落账事件重建后续阶段，不多付、不漏记。
   - JSONL 落盘后 `fsync`；重新打开时自动丢弃末尾撕裂行（写到一半的事件）。
7. **多方视图与全链路追溯**：
   - 出资方只看到自身合同、自身到账和必要的重叠摘要（对方仅显示资金类型与
     脱敏标识）；无利害关系的出资方看不到任何摘要。
   - 监管视图全局可见，并可用导出的事件序列经 `fold()` 独立复算。
   - 项目方可从**任一到账回单**经 `trace_release()` 追到：放款事件 → 资金
     合同与锁定事件 → 验收版本与成本项 → 审核决定与事件 → 重叠提示 → 立项申请。

## 使用示例

```python
from src.culture_finance_progress import ProgressLedger

led = ProgressLedger("journal.jsonl")   # 不传路径则为纯内存账本
led.submit_application("p1", "数字非遗展厅", request_id="app-1")
led.lock_fund(
    fund_id="F-BANK", subject_id="p1", funder_id="bank-001",
    fund_type="BANK_LOAN", amount="60", purpose="设备采购贷款",
    cost_items=[{"cost_item_id": "C-EQ", "name": "4K放映设备", "amount": "100"}],
    shares={"C-EQ": "60"},
    conditions=[{"condition_id": "G1", "label": "授信批复"}],
    request_id="lock-bank")
# ... 财政补贴 F-GOV 同比例锁定 40 ...

# 两边都按设备全额申报 → 产生重叠提示，但不自动决定
event, flags = led.submit_acceptance(
    acceptance_id="A1", subject_id="p1", submitted_by="proj-user",
    milestone="设备到场验收",
    requested={"C-EQ": {"F-BANK": "100", "F-GOV": "100"}},
    receipt_ids=["INV-1"], request_id="sub-a1")

# 独立审核人按 60/40 拆分定稿
led.review_acceptance(
    acceptance_id="A1", reviewer_id="auditor-1", decision="SPLIT",
    allocations={"C-EQ": {"F-BANK": "60", "F-GOV": "40"}},
    request_id="rev-a1")

led.mark_condition_met(fund_id="F-BANK", condition_id="G1")
led.release_tranche(fund_id="F-BANK", acceptance_id="A1",
                    amount="30", receipt_id="BK-1")   # 部分放款
led.release_tranche(fund_id="F-BANK", acceptance_id="A1",
                    amount="30", receipt_id="BK-1")   # 同回单：幂等返回原事件

assert led.recompute()["ok"]                            # 全局守恒复算
chain = led.trace_release("BK-1")                       # 回单 → 成本/验收/审批链
```

## 本地核对

```bash
python3 -m unittest discover -s tests
```

编译或构建：

```bash
python3 -m compileall -q .
```

所有测试和构建均在单个 Linux 应用容器内完成，不需要另行启动数据库或外部服务。
