"""穿透式进度账本：多方资金锁定、重叠提示、版本化验收、分批放款与追偿。

设计约束（对应资金平台主管的要求）：

1. 每笔资金合同锁定用途、成本项、出资比例与生效条件（CONTRACT_COMMITTED）；
2. 提交验收申请时自动识别跨资金来源的重复覆盖（OVERLAP_FLAGGED），
   但自动结果仅作提示，必须由独立审核人决定拆分（SPLIT）或拒绝（REJECT）
   （OVERLAP_RESOLVED）；
3. 部分验收只释放对应比例；后补票据与撤销结论通过新版本验收调整，
   原放款事件不被改写，差额通过追加放款或追偿核销；
4. 追偿与后续可用额度保持金额守恒：
   committed == available + released - recovered；
5. 出资方只看到自身合同与必要的脱敏重叠摘要，监管视图可复算全局；
6. 事件溯源 + 幂等键：重复回执、并发放款、服务中断后恢复都不多付、不漏记；
7. 项目方可从任一到账金额追到成本、验收与审批链。

金额一律使用 Decimal（两位小数），事件负载中的金额与比例以字符串保存，
保证事件日志可以直接 JSON 序列化交换。
"""

from __future__ import annotations

import threading
from decimal import Decimal, ROUND_HALF_UP

from src.culture_finance_progress import EVENT_KINDS

CENT = Decimal("0.01")
RATIO_QUANT = Decimal("0.0001")
ZERO = Decimal("0")

# 申请状态
APP_PENDING = "PENDING"      # 无未决提示，可验收
APP_FLAGGED = "FLAGGED"      # 存在未决重叠提示，验收被阻塞
APP_RESOLVED = "RESOLVED"    # 提示均已处理，可验收
APP_REJECTED = "REJECTED"    # 审核人拒绝，不可验收
APP_ACCEPTED = "ACCEPTED"    # 已有验收版本

FLAG_OPEN = "OPEN"
FLAG_RESOLVED = "RESOLVED"

# 审核决定
DECISION_SPLIT = "SPLIT"              # 拆分：按 allocation 重新分配各合同出资比例
DECISION_REJECT = "REJECT"            # 拒绝：驳回整份重复申报
DECISION_DISMISS = "DISMISS"          # 驳回提示：仅跨申请提示允许（如分批采购的误报）
DECISION_AUTO_CLOSED = "AUTO_CLOSED"  # 系统生成：申请被拒绝后自动关闭其余提示

# 重叠原因
REASON_CROSS_SOURCE = "CROSS_SOURCE"            # 跨资金来源重复覆盖
REASON_CROSS_APPLICATION = "CROSS_APPLICATION"  # 同一成本被重复申报

_OPEN_APP_STATUSES = (APP_PENDING, APP_FLAGGED, APP_RESOLVED, APP_ACCEPTED)


def _dec(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _money(value) -> Decimal:
    return _dec(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _ratio(value) -> Decimal:
    return _dec(value).quantize(RATIO_QUANT, rounding=ROUND_HALF_UP)


class LedgerError(Exception):
    """账本校验失败：请求被拒绝，不产生任何事件。"""


class ProgressLedger:
    """多方资金穿透式进度账本（事件溯源，线程安全）。

    所有状态均由事件日志推导；命令方法先校验再追加事件，校验失败抛
    LedgerError 且不落任何事件，因此中断恢复后重放同一日志必然得到
    相同状态。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._events: list[dict] = []
        self._seen: dict[str, dict] = {}
        self._contracts: dict[str, dict] = {}
        self._applications: dict[str, dict] = {}
        self._flags: dict[str, dict] = {}
        self._acceptances: dict[str, dict] = {}
        self._chains: dict[str, list[str]] = {}
        self._releases: dict[str, dict] = {}
        self._recoveries: dict[str, dict] = {}
        self._idem: dict[str, str] = {}

    # ------------------------------------------------------------------
    # 命令：资金合同登记
    # ------------------------------------------------------------------
    def commit_contract(self, *, event_id, occurred_at, contract_id, project_id,
                        funder_id, funder_kind, purpose, cost_items,
                        conditions=(), committed_amount):
        """登记资金合同，锁定用途、成本项、出资比例与生效条件。

        cost_items: {成本项: {"ratio": 出资比例(0,1], "cap": 该项最高覆盖额}}
        conditions: ["milestone:<里程碑id>"]，表示该里程碑验收通过后合同才生效。
        committed_amount 不得超过各成本项 cap 合计。
        """
        with self._lock:
            dup = self._duplicate(event_id)
            if dup is not None:
                return dup
            if contract_id in self._contracts:
                raise LedgerError(f"合同 {contract_id} 已存在")
            if not cost_items:
                raise LedgerError("合同必须锁定至少一个成本项")
            items: dict[str, dict] = {}
            total_cap = ZERO
            for item_id, spec in cost_items.items():
                ratio = _ratio(spec["ratio"])
                cap = _money(spec["cap"])
                if not (ZERO < ratio <= 1):
                    raise LedgerError(f"成本项 {item_id} 出资比例必须在 (0, 1]")
                if cap <= ZERO:
                    raise LedgerError(f"成本项 {item_id} 覆盖额度必须为正")
                items[item_id] = {"ratio": ratio, "cap": cap}
                total_cap += cap
            committed = _money(committed_amount)
            if committed <= ZERO or committed > total_cap:
                raise LedgerError("承诺金额必须为正且不超过成本项额度合计")
            conds = list(conditions)
            for cond in conds:
                if not (isinstance(cond, str) and cond.startswith("milestone:") and cond.split(":", 1)[1]):
                    raise LedgerError(f"无法识别的生效条件 {cond!r}，仅支持 milestone:<里程碑id>")
            event = self._event(event_id, "CONTRACT_COMMITTED", occurred_at, contract_id, {
                "contract_id": contract_id,
                "project_id": project_id,
                "funder_id": funder_id,
                "funder_kind": funder_kind,
                "purpose": purpose,
                "cost_items": {i: {"ratio": str(s["ratio"]), "cap": str(s["cap"])}
                               for i, s in items.items()},
                "conditions": conds,
                "committed_amount": str(committed),
            })
            return self._record(event)

    # ------------------------------------------------------------------
    # 命令：验收申请 + 自动重叠提示
    # ------------------------------------------------------------------
    def submit_application(self, *, event_id, occurred_at, application_id, project_id,
                           milestone_id, applicant_id, claims):
        """提交验收申请；系统自动识别重复覆盖并生成 OVERLAP_FLAGGED 提示。

        claims: [{"cost_item_id", "amount", "invoice_refs": [...]}]
        提示仅阻塞验收，不作任何自动处置，等待独立审核人决定。
        """
        with self._lock:
            dup = self._duplicate(event_id)
            if dup is not None:
                return dup
            if application_id in self._applications:
                raise LedgerError(f"申请 {application_id} 已存在")
            if not claims:
                raise LedgerError("验收申请必须包含至少一项成本")
            parsed: dict[str, dict] = {}
            for claim in claims:
                item = claim["cost_item_id"]
                amount = _money(claim["amount"])
                if amount <= ZERO:
                    raise LedgerError("申报金额必须为正")
                if item in parsed:
                    raise LedgerError(f"成本项 {item} 在同一申请中重复")
                covering = [c for c in self._contracts.values()
                            if c["project_id"] == project_id and item in c["cost_items"]]
                if not covering:
                    raise LedgerError(f"成本项 {item} 没有任何资金来源覆盖")
                parsed[item] = {"amount": amount,
                                "invoice_refs": list(claim.get("invoice_refs", []))}
            event = self._event(event_id, "APPLICATION_SUBMITTED", occurred_at, application_id, {
                "application_id": application_id,
                "project_id": project_id,
                "milestone_id": milestone_id,
                "applicant_id": applicant_id,
                "claims": [{"cost_item_id": i, "amount": str(c["amount"]),
                            "invoice_refs": c["invoice_refs"]} for i, c in parsed.items()],
            })
            self._record(event)
            flags = self._flag_overlaps(application_id, occurred_at, event_id)
            return {"status": "RECORDED", "event": event, "flags": flags}

    def _flag_overlaps(self, application_id, occurred_at, base_event_id):
        """对新申请做重复覆盖检查，生成提示事件（仅提示，不处置）。"""
        app = self._applications[application_id]
        flags: list[dict] = []
        seq = 0
        for item, claim in app["claims"].items():
            covering = [c for c in self._contracts.values()
                        if c["project_id"] == app["project_id"] and item in c["cost_items"]]
            total_ratio = sum((c["cost_items"][item]["ratio"] for c in covering), ZERO)
            if total_ratio > 1:
                seq += 1
                flag_id = f"{application_id}:{item}:{REASON_CROSS_SOURCE}"
                overlap = _money(claim["amount"] * (total_ratio - 1))
                flags.append(self._record(self._event(
                    f"{base_event_id}:flag:{seq}", "OVERLAP_FLAGGED", occurred_at, flag_id, {
                        "flag_id": flag_id,
                        "application_id": application_id,
                        "cost_item_id": item,
                        "reason": REASON_CROSS_SOURCE,
                        "claimed_amount": str(claim["amount"]),
                        "contracts": [{"contract_id": c["contract_id"],
                                       "funder_id": c["funder_id"],
                                       "ratio": str(c["cost_items"][item]["ratio"])}
                                      for c in covering],
                        "total_ratio": str(total_ratio),
                        "overlap_amount": str(overlap),
                    }))["event"])
            others = [a for a in self._applications.values()
                      if a["application_id"] != application_id
                      and a["project_id"] == app["project_id"]
                      and a["status"] in _OPEN_APP_STATUSES
                      and item in a["claims"]]
            if others:
                seq += 1
                flag_id = f"{application_id}:{item}:{REASON_CROSS_APPLICATION}"
                other_total = sum((a["claims"][item]["amount"] for a in others), ZERO)
                flags.append(self._record(self._event(
                    f"{base_event_id}:flag:{seq}", "OVERLAP_FLAGGED", occurred_at, flag_id, {
                        "flag_id": flag_id,
                        "application_id": application_id,
                        "cost_item_id": item,
                        "reason": REASON_CROSS_APPLICATION,
                        "claimed_amount": str(claim["amount"]),
                        "contracts": [{"contract_id": c["contract_id"],
                                       "funder_id": c["funder_id"],
                                       "ratio": str(c["cost_items"][item]["ratio"])}
                                      for c in covering],
                        "other_application_ids": [a["application_id"] for a in others],
                        "other_claimed_total": str(other_total),
                        "overlap_amount": str(min(claim["amount"], other_total)),
                    }))["event"])
        return flags

    # ------------------------------------------------------------------
    # 命令：独立审核人处置重叠提示
    # ------------------------------------------------------------------
    def resolve_overlap(self, *, event_id, occurred_at, flag_id, reviewer_id, decision,
                        allocation=None, reason=""):
        """独立审核人对重叠提示作出拆分/拒绝/驳回决定。

        SPLIT  仅用于跨资金来源提示：allocation 给出各合同对该成本项的
               实际出资比例，合计必须在 (0, 1]，未列出的合同视为 0；
        REJECT 拒绝整份重复申报，该申请不可再验收，其余未决提示自动关闭；
        DISMISS 仅用于跨申请提示：确认并非重复（如分批采购），放行申请。
        审核人不得为申请人本人，也不得为相关合同的出资方。
        """
        with self._lock:
            dup = self._duplicate(event_id)
            if dup is not None:
                return dup
            flag = self._flags.get(flag_id)
            if flag is None:
                raise LedgerError(f"重叠提示 {flag_id} 不存在")
            if flag["status"] != FLAG_OPEN:
                raise LedgerError("该提示已处理")
            app = self._applications[flag["application_id"]]
            if reviewer_id == app["applicant_id"]:
                raise LedgerError("审核人不得为申请人本人")
            involved_funders = {self._contracts[c]["funder_id"] for c in flag["contracts"]}
            if reviewer_id in involved_funders:
                raise LedgerError("审核人不得为相关出资方")
            payload = {"flag_id": flag_id, "application_id": flag["application_id"],
                       "reviewer_id": reviewer_id, "decision": decision, "reason": reason}
            if decision == DECISION_SPLIT:
                if flag["reason"] != REASON_CROSS_SOURCE:
                    raise LedgerError("跨申请重复申报不能拆分，只能拒绝或驳回提示")
                alloc = {cid: _ratio(r) for cid, r in (allocation or {}).items()}
                unknown = sorted(set(alloc) - set(flag["contracts"]))
                if unknown:
                    raise LedgerError(f"拆分包含未涉及合同: {unknown}")
                if any(r < ZERO for r in alloc.values()):
                    raise LedgerError("拆分比例不得为负")
                total = sum(alloc.values(), ZERO)
                if total <= ZERO or total > 1:
                    raise LedgerError("拆分比例合计必须在 (0, 1]")
                payload["allocation"] = {cid: str(alloc.get(cid, ZERO))
                                         for cid in flag["contracts"]}
            elif decision == DECISION_DISMISS:
                if flag["reason"] != REASON_CROSS_APPLICATION:
                    raise LedgerError("只有跨申请提示可以驳回")
            elif decision != DECISION_REJECT:
                raise LedgerError(f"未知决定 {decision!r}")
            result = self._record(self._event(event_id, "OVERLAP_RESOLVED",
                                              occurred_at, flag_id, payload))
            if decision == DECISION_REJECT:
                # 其余未决提示由系统生成 AUTO_CLOSED 决定事件，保证日志完整可重放。
                for other in list(self._flags.values()):
                    if (other["application_id"] == flag["application_id"]
                            and other["status"] == FLAG_OPEN):
                        self._record(self._event(
                            f"{event_id}:auto:{other['flag_id']}", "OVERLAP_RESOLVED",
                            occurred_at, other["flag_id"], {
                                "flag_id": other["flag_id"],
                                "application_id": other["application_id"],
                                "reviewer_id": reviewer_id,
                                "decision": DECISION_AUTO_CLOSED,
                                "reason": "申请已被拒绝，提示自动关闭",
                            }))
            return result

    # ------------------------------------------------------------------
    # 命令：里程碑验收（版本化）
    # ------------------------------------------------------------------
    def accept_milestone(self, *, event_id, occurred_at, acceptance_id, application_id,
                         accepted_ratio="1", adjusted_claims=None, reason=""):
        """登记一个验收版本。

        首版本 accepted_ratio ∈ (0, 1]（部分验收只释放对应比例）；
        后续版本（后补票据/撤销结论）必须给出 reason，accepted_ratio ∈ [0, 1]，
        adjusted_claims 完整重述生效申报额（不继承上一版本），原放款事件不改写。
        """
        with self._lock:
            dup = self._duplicate(event_id)
            if dup is not None:
                return dup
            app = self._applications.get(application_id)
            if app is None:
                raise LedgerError(f"申请 {application_id} 不存在")
            if app["status"] == APP_REJECTED:
                raise LedgerError("申请已被审核人拒绝，不能验收")
            if app["status"] == APP_FLAGGED:
                raise LedgerError("存在未处理的重叠提示，需独立审核人先行决定")
            if acceptance_id in self._acceptances:
                raise LedgerError(f"验收 {acceptance_id} 已存在")
            ratio = _ratio(accepted_ratio)
            chain = self._chains.get(application_id, [])
            if not chain:
                if not (ZERO < ratio <= 1):
                    raise LedgerError("首次验收比例必须在 (0, 1]")
                version, supersedes = 1, None
            else:
                if not (ZERO <= ratio <= 1):
                    raise LedgerError("验收比例必须在 [0, 1]")
                if not reason:
                    raise LedgerError("版本化调整必须说明原因（后补票据/撤销结论）")
                version = self._acceptances[chain[-1]]["version"] + 1
                supersedes = chain[-1]
            base = {item: c["amount"] for item, c in app["claims"].items()}
            adjusted = None
            if adjusted_claims is not None:
                adjusted = {}
                for item, amount in adjusted_claims.items():
                    if item not in base:
                        raise LedgerError(f"调整包含未申报成本项 {item}")
                    amt = _money(amount)
                    if amt <= ZERO:
                        raise LedgerError("调整后金额必须为正")
                    adjusted[item] = amt
            effective = {item: (adjusted or {}).get(item, amt) for item, amt in base.items()}
            accepted = {item: _money(amt * ratio) for item, amt in effective.items()}
            event = self._event(event_id, "MILESTONE_ACCEPTED", occurred_at, acceptance_id, {
                "acceptance_id": acceptance_id,
                "application_id": application_id,
                "project_id": app["project_id"],
                "milestone_id": app["milestone_id"],
                "version": version,
                "supersedes": supersedes,
                "accepted_ratio": str(ratio),
                "adjusted_claims": ({i: str(a) for i, a in adjusted.items()}
                                    if adjusted else None),
                "accepted_amounts": {i: str(a) for i, a in accepted.items()},
                "reason": reason,
            })
            return self._record(event)

    # ------------------------------------------------------------------
    # 命令：分批放款
    # ------------------------------------------------------------------
    def release_tranche(self, *, event_id, occurred_at, release_id, contract_id,
                        application_id, idempotency_key):
        """按当前验收版本对合同放款，放款额 = 覆盖额 - 该申请链已放额。

        idempotency_key 去重保证重复回执不产生二次放款；额度校验与事件追加
        在同一锁内完成，并发放款不会多付。
        """
        with self._lock:
            dup = self._duplicate(event_id)
            if dup is not None:
                return dup
            if idempotency_key in self._idem:
                existing = self._releases[self._idem[idempotency_key]]
                if (existing["contract_id"] != contract_id
                        or existing["application_id"] != application_id):
                    raise LedgerError("幂等键已被其他放款使用")
                return {"status": "DUPLICATE", "event": self._seen[existing["event_id"]]}
            contract = self._contracts.get(contract_id)
            if contract is None:
                raise LedgerError(f"合同 {contract_id} 不存在")
            app = self._applications.get(application_id)
            if app is None:
                raise LedgerError(f"申请 {application_id} 不存在")
            if contract["project_id"] != app["project_id"]:
                raise LedgerError("合同与申请不属于同一项目")
            chain = self._chains.get(application_id, [])
            if not chain:
                raise LedgerError("里程碑尚未验收，不能放款")
            for cond in contract["conditions"]:
                milestone_id = cond.split(":", 1)[1]
                if not self._milestone_accepted(contract["project_id"], milestone_id):
                    raise LedgerError(f"生效条件未满足：里程碑 {milestone_id} 未验收")
            coverage = self._coverage(contract_id, application_id)
            if not coverage:
                raise LedgerError("合同不覆盖该申请的任何成本项")
            released = self._released_items(contract_id, application_id)
            breakdown = {item: cov - released.get(item, ZERO)
                         for item, cov in coverage.items()}
            due = sum(breakdown.values(), ZERO)
            if due < ZERO:
                raise LedgerError("验收金额已下调，超出部分需通过追偿核销")
            if due == ZERO:
                raise LedgerError("没有待放款金额")
            available = contract["committed"] - (contract["released"] - contract["recovered"])
            if due > available:
                raise LedgerError(f"放款 {due} 超出合同可用额度 {available}")
            for item, cov in coverage.items():
                cap = contract["cost_items"][item]["cap"]
                if cov > cap:
                    raise LedgerError(f"成本项 {item} 累计覆盖 {cov} 超过合同额度 {cap}")
            current = self._acceptances[chain[-1]]
            event = self._event(event_id, "TRANCHE_RELEASED", occurred_at, release_id, {
                "release_id": release_id,
                "contract_id": contract_id,
                "application_id": application_id,
                "acceptance_id": current["acceptance_id"],
                "project_id": contract["project_id"],
                "funder_id": contract["funder_id"],
                "amount": str(due),
                "idempotency_key": idempotency_key,
                "cost_breakdown": [{"cost_item_id": i, "amount": str(a)}
                                   for i, a in breakdown.items()],
            })
            return self._record(event)

    # ------------------------------------------------------------------
    # 命令：追偿核销
    # ------------------------------------------------------------------
    def reconcile_recovery(self, *, event_id, occurred_at, recovery_id, contract_id,
                           application_id, amount, reason=""):
        """核销追偿：验收版本下调后，把超付金额收回合同可用额度。

        追偿金额不得超过待追偿余额（已放 - 当前覆盖 - 已追偿），
        保证 committed == available + released - recovered 守恒。
        """
        with self._lock:
            dup = self._duplicate(event_id)
            if dup is not None:
                return dup
            contract = self._contracts.get(contract_id)
            if contract is None:
                raise LedgerError(f"合同 {contract_id} 不存在")
            if application_id not in self._applications:
                raise LedgerError(f"申请 {application_id} 不存在")
            amount = _money(amount)
            if amount <= ZERO:
                raise LedgerError("追偿金额必须为正")
            coverage_total = sum(self._coverage(contract_id, application_id).values(), ZERO)
            released_total = sum(self._released_items(contract_id, application_id).values(), ZERO)
            outstanding = released_total - coverage_total - self._recovered_for(contract_id, application_id)
            if outstanding <= ZERO:
                raise LedgerError("当前没有超付金额，无需追偿")
            if amount > outstanding:
                raise LedgerError(f"追偿金额 {amount} 超出待追偿余额 {outstanding}")
            event = self._event(event_id, "RECOVERY_RECONCILED", occurred_at, recovery_id, {
                "recovery_id": recovery_id,
                "contract_id": contract_id,
                "application_id": application_id,
                "project_id": contract["project_id"],
                "funder_id": contract["funder_id"],
                "amount": str(amount),
                "reason": reason,
            })
            return self._record(event)

    # ------------------------------------------------------------------
    # 查询：头寸、守恒、状态
    # ------------------------------------------------------------------
    def contract_position(self, contract_id) -> dict:
        """合同头寸：committed / released / recovered / available（Decimal）。"""
        with self._lock:
            contract = self._contracts.get(contract_id)
            if contract is None:
                raise LedgerError(f"合同 {contract_id} 不存在")
            net = contract["released"] - contract["recovered"]
            return {
                "contract_id": contract_id,
                "funder_id": contract["funder_id"],
                "committed": contract["committed"],
                "released": contract["released"],
                "recovered": contract["recovered"],
                "net_released": net,
                "available": contract["committed"] - net,
            }

    def application_status(self, application_id) -> str:
        with self._lock:
            app = self._applications.get(application_id)
            if app is None:
                raise LedgerError(f"申请 {application_id} 不存在")
            return app["status"]

    def verify_conservation(self) -> list[str]:
        """金额守恒校验，返回问题列表（为空表示守恒）。"""
        with self._lock:
            problems: list[str] = []
            for c in self._contracts.values():
                net = c["released"] - c["recovered"]
                if net < ZERO:
                    problems.append(f"合同 {c['contract_id']} 追偿超过放款")
                if net > c["committed"]:
                    problems.append(f"合同 {c['contract_id']} 净放款超过承诺金额")
            for app_id, chain in self._chains.items():
                if not chain:
                    continue
                app = self._applications[app_id]
                current = self._acceptances[chain[-1]]
                for item, accepted in current["accepted_amounts"].items():
                    total, n = ZERO, 0
                    for c in self._contracts.values():
                        if c["project_id"] != app["project_id"] or item not in c["cost_items"]:
                            continue
                        cov = self._coverage(c["contract_id"], app_id).get(item)
                        if cov:
                            total += cov
                            n += 1
                    if total - accepted > CENT * max(n, 1):
                        problems.append(
                            f"申请 {app_id} 成本项 {item} 覆盖 {total} 超过验收额 {accepted}")
            for c in self._contracts.values():
                for app_id in self._applications:
                    recovered = self._recovered_for(c["contract_id"], app_id)
                    if recovered <= ZERO:
                        continue
                    released = sum(self._released_items(c["contract_id"], app_id).values(), ZERO)
                    coverage = sum(self._coverage(c["contract_id"], app_id).values(), ZERO)
                    if recovered - max(ZERO, released - coverage) > ZERO:
                        problems.append(
                            f"合同 {c['contract_id']} 对申请 {app_id} 追偿超过超付额")
            return problems

    # ------------------------------------------------------------------
    # 视图：出资方 / 项目方 / 监管
    # ------------------------------------------------------------------
    def funder_view(self, funder_id) -> list[dict]:
        """出资方视图：仅自身合同、自身放款/追偿，以及脱敏后的重叠摘要。"""
        with self._lock:
            own = {cid for cid, c in self._contracts.items() if c["funder_id"] == funder_id}
            view: list[dict] = []
            for event in self._events:
                kind, p = event["kind"], event["payload"]
                if kind == "CONTRACT_COMMITTED" and p["contract_id"] in own:
                    view.append(event)
                elif kind in ("TRANCHE_RELEASED", "RECOVERY_RECONCILED") \
                        and p["contract_id"] in own:
                    view.append(event)
                elif kind == "OVERLAP_FLAGGED":
                    mine = [c["contract_id"] for c in p.get("contracts", [])
                            if c["contract_id"] in own]
                    if mine:
                        view.append(self._redact_flag(event, mine))
                elif kind == "OVERLAP_RESOLVED":
                    flag = self._flags.get(p["flag_id"])
                    if flag and any(c in own for c in flag["contracts"]):
                        view.append(self._redact_resolution(event, own))
            return view

    def project_view(self, project_id) -> list[dict]:
        """项目方视图：本项目的合同、申请、验收、提示、决定、放款与追偿。"""
        with self._lock:
            view = []
            for event in self._events:
                p = event["payload"]
                if p.get("project_id") == project_id:
                    view.append(event)
                elif event["kind"] in ("OVERLAP_FLAGGED", "OVERLAP_RESOLVED"):
                    app = self._applications.get(p.get("application_id"))
                    if app and app["project_id"] == project_id:
                        view.append(event)
            return view

    def regulator_view(self) -> dict:
        """监管视图：完整事件日志、各合同头寸，并重放日志复算全局。"""
        with self._lock:
            positions = {}
            for cid in self._contracts:
                pos = self.contract_position(cid)
                positions[cid] = {k: (str(v) if isinstance(v, Decimal) else v)
                                  for k, v in pos.items()}
            return {
                "events": list(self._events),
                "positions": positions,
                "conservation_problems": self.verify_conservation(),
                "audit_problems": self.audit(self._events),
            }

    @staticmethod
    def _json_resolution(resolution):
        if resolution is None:
            return None
        return {"decision": resolution["decision"],
                "reviewer_id": resolution["reviewer_id"],
                "reason": resolution["reason"],
                "allocation": ({cid: str(r) for cid, r in resolution["allocation"].items()}
                               if resolution["allocation"] else None)}

    @staticmethod
    def _redact_flag(event, mine_ids):
        p = event["payload"]
        own_ratio = next((c["ratio"] for c in p.get("contracts", [])
                          if c["contract_id"] in mine_ids), None)
        others = len(p.get("contracts", [])) - len(mine_ids)
        payload = {
            "flag_id": p["flag_id"],
            "application_id": p["application_id"],
            "cost_item_id": p["cost_item_id"],
            "reason": p["reason"],
            "claimed_amount": p["claimed_amount"],
            "overlap_amount": p["overlap_amount"],
            "own_contract_ids": list(mine_ids),
            "own_ratio": own_ratio,
            "other_party_count": others,
            "summary": f"成本项 {p['cost_item_id']} 存在重复覆盖提示，"
                       f"涉及 {others} 个其他出资方，待独立审核人处理",
        }
        if "total_ratio" in p:
            payload["total_ratio"] = p["total_ratio"]
        if "other_application_ids" in p:
            payload["related_application_count"] = len(p["other_application_ids"])
        return {**event, "payload": payload}

    @staticmethod
    def _redact_resolution(event, own):
        p = event["payload"]
        payload = {"flag_id": p["flag_id"], "application_id": p["application_id"],
                   "reviewer_id": p["reviewer_id"], "decision": p["decision"],
                   "reason": p.get("reason", "")}
        if "allocation" in p:
            own_alloc = {cid: r for cid, r in p["allocation"].items() if cid in own}
            payload["own_allocation"] = own_alloc
            payload["other_party_count"] = len(p["allocation"]) - len(own_alloc)
        return {**event, "payload": payload}

    # ------------------------------------------------------------------
    # 追溯：从到账金额追到成本、验收与审批链
    # ------------------------------------------------------------------
    def trace_release(self, release_id) -> dict:
        """从一笔到账（放款）反查成本构成、验收版本链与审批链。"""
        with self._lock:
            record = self._releases.get(release_id)
            if record is None:
                raise LedgerError(f"放款 {release_id} 不存在")
            contract = self._contracts[record["contract_id"]]
            app = self._applications[record["application_id"]]
            chain = [self._acceptances[a] for a in self._chains[record["application_id"]]]
            flags = [f for f in self._flags.values()
                     if f["application_id"] == record["application_id"]]
            current = chain[-1]
            breakdown = []
            for item, amount in record["breakdown"].items():
                spec = contract["cost_items"].get(item)
                declared = spec["ratio"] if spec else None
                breakdown.append({
                    "cost_item_id": item,
                    "released_amount": str(amount),
                    "accepted_amount": str(current["accepted_amounts"].get(item, ZERO)),
                    "effective_ratio": str(self._effective_ratio(
                        record["contract_id"], record["application_id"], item, declared)),
                })
            recoveries = [{"recovery_id": r["recovery_id"], "amount": str(r["amount"]),
                           "reason": r["reason"]}
                          for r in self._recoveries.values()
                          if r["contract_id"] == record["contract_id"]
                          and r["application_id"] == record["application_id"]]
            return {
                "release": {"release_id": release_id,
                            "amount": str(record["amount"]),
                            "acceptance_id": record["acceptance_id"],
                            "idempotency_key": record["idempotency_key"]},
                "contract": {"contract_id": contract["contract_id"],
                             "funder_id": contract["funder_id"],
                             "funder_kind": contract["funder_kind"],
                             "purpose": contract["purpose"]},
                "application": {"application_id": app["application_id"],
                                "milestone_id": app["milestone_id"],
                                "applicant_id": app["applicant_id"],
                                "claims": {i: str(c["amount"])
                                           for i, c in app["claims"].items()}},
                "acceptance_chain": [{"acceptance_id": a["acceptance_id"],
                                      "version": a["version"],
                                      "supersedes": a["supersedes"],
                                      "accepted_ratio": str(a["ratio"]),
                                      "accepted_amounts": {i: str(v) for i, v in
                                                           a["accepted_amounts"].items()},
                                      "reason": a["reason"]} for a in chain],
                "approvals": [{"flag_id": f["flag_id"], "reason": f["reason"],
                               "status": f["status"],
                               "resolution": self._json_resolution(f["resolution"])}
                              for f in flags],
                "cost_breakdown": breakdown,
                "recoveries": recoveries,
            }

    # ------------------------------------------------------------------
    # 持久化与恢复：事件日志、重放、监管复算
    # ------------------------------------------------------------------
    @property
    def events(self) -> list[dict]:
        """完整事件日志（可持久化，用于中断后恢复）。"""
        return list(self._events)

    @classmethod
    def replay(cls, events) -> "ProgressLedger":
        """从事事件日志恢复账本；重复的 event_id 会被去重，不多付不漏记。"""
        ledger = cls()
        for event in events:
            if event["event_id"] in ledger._seen:
                continue
            ledger._events.append(event)
            ledger._seen[event["event_id"]] = event
            ledger._apply(event)
        return ledger

    @classmethod
    def audit(cls, events) -> list[str]:
        """监管复算：重放日志并重新计算放款/追偿金额，返回不一致问题。"""
        ledger = cls()
        problems: list[str] = []
        for event in events:
            try:
                ledger._apply_verified(event)
            except (LedgerError, KeyError) as exc:
                problems.append(f"事件 {event.get('event_id')}: {exc}")
        problems.extend(ledger.verify_conservation())
        return problems

    # ------------------------------------------------------------------
    # 内部：事件应用（重放与命令共用，保证状态完全由日志推导）
    # ------------------------------------------------------------------
    @staticmethod
    def _event(event_id, kind, occurred_at, subject_id, payload) -> dict:
        return {"event_id": event_id, "kind": kind, "occurred_at": occurred_at,
                "subject_id": subject_id, "payload": payload}

    def _record(self, event) -> dict:
        self._events.append(event)
        self._seen[event["event_id"]] = event
        self._apply(event)
        return {"status": "RECORDED", "event": event}

    def _duplicate(self, event_id):
        if event_id in self._seen:
            return {"status": "DUPLICATE", "event": self._seen[event_id]}
        return None

    def _apply(self, event) -> None:
        kind = event["kind"]
        if kind not in EVENT_KINDS:
            raise LedgerError(f"未知事件类型 {kind!r}")
        p = event["payload"]
        if kind == "CONTRACT_COMMITTED":
            self._contracts[p["contract_id"]] = {
                "contract_id": p["contract_id"],
                "project_id": p["project_id"],
                "funder_id": p["funder_id"],
                "funder_kind": p["funder_kind"],
                "purpose": p["purpose"],
                "cost_items": {i: {"ratio": _ratio(s["ratio"]), "cap": _money(s["cap"])}
                               for i, s in p["cost_items"].items()},
                "conditions": list(p["conditions"]),
                "committed": _money(p["committed_amount"]),
                "released": ZERO,
                "recovered": ZERO,
            }
        elif kind == "APPLICATION_SUBMITTED":
            self._applications[p["application_id"]] = {
                "application_id": p["application_id"],
                "project_id": p["project_id"],
                "milestone_id": p["milestone_id"],
                "applicant_id": p["applicant_id"],
                "claims": {c["cost_item_id"]: {"amount": _money(c["amount"]),
                                              "invoice_refs": list(c.get("invoice_refs", []))}
                           for c in p["claims"]},
                "status": APP_PENDING,
            }
            self._chains.setdefault(p["application_id"], [])
        elif kind == "OVERLAP_FLAGGED":
            self._flags[p["flag_id"]] = {
                "flag_id": p["flag_id"],
                "application_id": p["application_id"],
                "cost_item_id": p["cost_item_id"],
                "reason": p["reason"],
                "contracts": [c["contract_id"] for c in p.get("contracts", [])],
                "claimed_amount": _money(p["claimed_amount"]),
                "overlap_amount": _money(p["overlap_amount"]),
                "other_application_ids": list(p.get("other_application_ids", [])),
                "status": FLAG_OPEN,
                "resolution": None,
            }
            app = self._applications[p["application_id"]]
            if app["status"] == APP_PENDING:
                app["status"] = APP_FLAGGED
        elif kind == "OVERLAP_RESOLVED":
            flag = self._flags[p["flag_id"]]
            flag["status"] = FLAG_RESOLVED
            flag["resolution"] = {
                "decision": p["decision"],
                "reviewer_id": p["reviewer_id"],
                "reason": p.get("reason", ""),
                "allocation": ({cid: _ratio(r) for cid, r in p["allocation"].items()}
                               if "allocation" in p else None),
            }
            app = self._applications[flag["application_id"]]
            if p["decision"] == DECISION_REJECT:
                app["status"] = APP_REJECTED
            elif app["status"] == APP_FLAGGED and all(
                    f["status"] == FLAG_RESOLVED for f in self._flags.values()
                    if f["application_id"] == app["application_id"]):
                app["status"] = APP_RESOLVED
        elif kind == "MILESTONE_ACCEPTED":
            self._acceptances[p["acceptance_id"]] = {
                "acceptance_id": p["acceptance_id"],
                "application_id": p["application_id"],
                "version": int(p["version"]),
                "supersedes": p["supersedes"],
                "ratio": _ratio(p["accepted_ratio"]),
                "adjusted_claims": ({i: _money(a) for i, a in p["adjusted_claims"].items()}
                                    if p.get("adjusted_claims") else None),
                "accepted_amounts": {i: _money(a) for i, a in p["accepted_amounts"].items()},
                "reason": p.get("reason", ""),
            }
            self._chains[p["application_id"]].append(p["acceptance_id"])
            self._applications[p["application_id"]]["status"] = APP_ACCEPTED
        elif kind == "TRANCHE_RELEASED":
            breakdown = {b["cost_item_id"]: _money(b["amount"]) for b in p["cost_breakdown"]}
            self._releases[p["release_id"]] = {
                "release_id": p["release_id"],
                "contract_id": p["contract_id"],
                "application_id": p["application_id"],
                "acceptance_id": p["acceptance_id"],
                "amount": _money(p["amount"]),
                "breakdown": breakdown,
                "idempotency_key": p["idempotency_key"],
                "event_id": event["event_id"],
            }
            self._contracts[p["contract_id"]]["released"] += _money(p["amount"])
            self._idem[p["idempotency_key"]] = p["release_id"]
        elif kind == "RECOVERY_RECONCILED":
            self._recoveries[p["recovery_id"]] = {
                "recovery_id": p["recovery_id"],
                "contract_id": p["contract_id"],
                "application_id": p["application_id"],
                "amount": _money(p["amount"]),
                "reason": p.get("reason", ""),
            }
            self._contracts[p["contract_id"]]["recovered"] += _money(p["amount"])

    def _apply_verified(self, event) -> None:
        """监管复算用：应用前重新计算关键金额，与事件记录比对。"""
        kind = event["kind"]
        p = event["payload"]
        if kind == "MILESTONE_ACCEPTED":
            chain = self._chains.get(p["application_id"], [])
            expected = chain[-1] if chain else None
            if p["supersedes"] != expected:
                raise LedgerError("验收版本链断裂")
        elif kind == "TRANCHE_RELEASED":
            if p["idempotency_key"] in self._idem:
                raise LedgerError(f"幂等键 {p['idempotency_key']} 重复")
            coverage = self._coverage(p["contract_id"], p["application_id"])
            released = self._released_items(p["contract_id"], p["application_id"])
            expected = sum(coverage.values(), ZERO) - sum(released.values(), ZERO)
            if _money(p["amount"]) != expected:
                raise LedgerError(f"放款金额 {p['amount']} 与复算结果 {expected} 不一致")
        elif kind == "RECOVERY_RECONCILED":
            coverage = sum(self._coverage(p["contract_id"], p["application_id"]).values(), ZERO)
            released = sum(self._released_items(p["contract_id"], p["application_id"]).values(), ZERO)
            outstanding = released - coverage - self._recovered_for(p["contract_id"],
                                                                    p["application_id"])
            if not (ZERO < _money(p["amount"]) <= outstanding):
                raise LedgerError(f"追偿金额 {p['amount']} 超出待追偿余额 {outstanding}")
        self._apply(event)

    # ------------------------------------------------------------------
    # 内部：派生计算
    # ------------------------------------------------------------------
    def _coverage(self, contract_id, application_id) -> dict:
        """当前验收版本下，合同对该申请链各成本项的覆盖金额。"""
        contract = self._contracts[contract_id]
        chain = self._chains.get(application_id, [])
        if not chain:
            return {}
        current = self._acceptances[chain[-1]]
        result = {}
        for item, accepted in current["accepted_amounts"].items():
            spec = contract["cost_items"].get(item)
            if spec is None:
                continue
            ratio = self._effective_ratio(contract_id, application_id, item, spec["ratio"])
            result[item] = _money(accepted * ratio)
        return result

    def _effective_ratio(self, contract_id, application_id, item, declared):
        """实际出资比例：若该申请该成本项已被审核人拆分，用拆分比例。"""
        for flag in self._flags.values():
            if (flag["application_id"] == application_id
                    and flag["cost_item_id"] == item
                    and flag["reason"] == REASON_CROSS_SOURCE
                    and flag["status"] == FLAG_RESOLVED
                    and flag["resolution"]
                    and flag["resolution"]["decision"] == DECISION_SPLIT):
                return flag["resolution"]["allocation"].get(contract_id, ZERO)
        return declared

    def _released_items(self, contract_id, application_id) -> dict:
        items: dict[str, Decimal] = {}
        for r in self._releases.values():
            if r["contract_id"] == contract_id and r["application_id"] == application_id:
                for item, amt in r["breakdown"].items():
                    items[item] = items.get(item, ZERO) + amt
        return items

    def _recovered_for(self, contract_id, application_id) -> Decimal:
        total = ZERO
        for r in self._recoveries.values():
            if r["contract_id"] == contract_id and r["application_id"] == application_id:
                total += r["amount"]
        return total

    def _milestone_accepted(self, project_id, milestone_id) -> bool:
        for app in self._applications.values():
            if app["project_id"] != project_id or app["milestone_id"] != milestone_id:
                continue
            chain = self._chains.get(app["application_id"], [])
            if chain and self._acceptances[chain[-1]]["ratio"] > ZERO:
                return True
        return False


__all__ = [
    "ProgressLedger",
    "LedgerError",
    "DECISION_SPLIT",
    "DECISION_REJECT",
    "DECISION_DISMISS",
    "REASON_CROSS_SOURCE",
    "REASON_CROSS_APPLICATION",
]
