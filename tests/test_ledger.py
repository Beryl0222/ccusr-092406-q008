"""穿透式进度账本测试。

场景主线：文化企业 PRJ-1 同时获得银行贷款（CT-BANK）、财政补贴（CT-SUB）
与社会投资（CT-INV）。设备成本项 EQUIP 被银行与财政各按 60% 覆盖，
同一成本被覆盖两次，由独立审核人拆分后按比例放款；后续后补票据
（新版本 v2）与撤销结论（新版本 v3）通过追加放款与追偿保持金额守恒。
"""

import copy
import itertools
import json
import threading
import unittest
from decimal import Decimal

from src.culture_finance_progress import EVENT_KINDS, validate_event
from src.ledger import LedgerError, ProgressLedger

_seq = itertools.count(1)


def eid():
    return f"e-{next(_seq):04d}"


def ts():
    return f"2026-09-25T10:{next(_seq) % 60:02d}:00+08:00"


def new_ledger():
    """三份合同：银行/财政各按 60% 覆盖设备（重叠），社会投资覆盖内容。"""
    ledger = ProgressLedger()
    ledger.commit_contract(
        event_id=eid(), occurred_at=ts(), contract_id="CT-BANK", project_id="PRJ-1",
        funder_id="BANK-A", funder_kind="BANK_LOAN", purpose="设备购置",
        cost_items={"EQUIP": {"ratio": "0.6", "cap": "600000"}},
        conditions=[], committed_amount="600000")
    ledger.commit_contract(
        event_id=eid(), occurred_at=ts(), contract_id="CT-SUB", project_id="PRJ-1",
        funder_id="FIN-BUREAU", funder_kind="FISCAL_SUBSIDY", purpose="设备购置",
        cost_items={"EQUIP": {"ratio": "0.6", "cap": "600000"}},
        conditions=[], committed_amount="600000")
    ledger.commit_contract(
        event_id=eid(), occurred_at=ts(), contract_id="CT-INV", project_id="PRJ-1",
        funder_id="FUND-C", funder_kind="SOCIAL_INVESTMENT", purpose="内容制作",
        cost_items={"CONTENT": {"ratio": "1", "cap": "300000"}},
        conditions=[], committed_amount="300000")
    return ledger


def submit_equipment(ledger, app_id="APP-1", amount="1000000"):
    return ledger.submit_application(
        event_id=eid(), occurred_at=ts(), application_id=app_id, project_id="PRJ-1",
        milestone_id="M2", applicant_id="PM-1",
        claims=[{"cost_item_id": "EQUIP", "amount": amount, "invoice_refs": ["INV-001"]}])


def split_first_flag(ledger, res):
    flag_id = res["flags"][0]["payload"]["flag_id"]
    return ledger.resolve_overlap(
        event_id=eid(), occurred_at=ts(), flag_id=flag_id, reviewer_id="REV-1",
        decision="SPLIT", allocation={"CT-BANK": "0.5", "CT-SUB": "0.5"},
        reason="各承担一半")


def accepted_ledger():
    """合同 + 申请 + 审核拆分 + 50% 部分验收。"""
    ledger = new_ledger()
    res = submit_equipment(ledger)
    split_first_flag(ledger, res)
    ledger.accept_milestone(event_id=eid(), occurred_at=ts(), acceptance_id="ACC-1",
                            application_id="APP-1", accepted_ratio="0.5")
    return ledger


def released_ledger():
    """在部分验收后，银行与财政各放第一批款。"""
    ledger = accepted_ledger()
    ledger.release_tranche(event_id=eid(), occurred_at=ts(), release_id="REL-B1",
                           contract_id="CT-BANK", application_id="APP-1",
                           idempotency_key="CT-BANK:APP-1:1")
    ledger.release_tranche(event_id=eid(), occurred_at=ts(), release_id="REL-S1",
                           contract_id="CT-SUB", application_id="APP-1",
                           idempotency_key="CT-SUB:APP-1:1")
    return ledger


def full_scenario():
    """完整链路：后补票据（v2）追加放款，撤销结论（v3）触发追偿。"""
    ledger = released_ledger()
    ledger.accept_milestone(event_id=eid(), occurred_at=ts(), acceptance_id="ACC-2",
                            application_id="APP-1", accepted_ratio="1",
                            adjusted_claims={"EQUIP": "1100000"}, reason="后补票据 INV-002")
    ledger.release_tranche(event_id=eid(), occurred_at=ts(), release_id="REL-B2",
                           contract_id="CT-BANK", application_id="APP-1",
                           idempotency_key="CT-BANK:APP-1:2")
    ledger.release_tranche(event_id=eid(), occurred_at=ts(), release_id="REL-S2",
                           contract_id="CT-SUB", application_id="APP-1",
                           idempotency_key="CT-SUB:APP-1:2")
    ledger.accept_milestone(event_id=eid(), occurred_at=ts(), acceptance_id="ACC-3",
                            application_id="APP-1", accepted_ratio="1",
                            adjusted_claims={"EQUIP": "1000000"}, reason="撤销部分结论")
    ledger.reconcile_recovery(event_id=eid(), occurred_at=ts(), recovery_id="REC-B1",
                              contract_id="CT-BANK", application_id="APP-1",
                              amount="50000", reason="追回超付")
    ledger.reconcile_recovery(event_id=eid(), occurred_at=ts(), recovery_id="REC-S1",
                              contract_id="CT-SUB", application_id="APP-1",
                              amount="50000", reason="追回超付")
    return ledger


class OverlapDetectionTest(unittest.TestCase):
    def test_overlap_flagged_as_hint_and_blocks_acceptance(self):
        ledger = new_ledger()
        res = submit_equipment(ledger)
        self.assertEqual(len(res["flags"]), 1)
        flag = res["flags"][0]["payload"]
        self.assertEqual(flag["reason"], "CROSS_SOURCE")
        self.assertEqual(flag["total_ratio"], "1.2000")
        self.assertEqual(flag["overlap_amount"], "200000.00")
        self.assertEqual(ledger.application_status("APP-1"), "FLAGGED")
        # 自动结果只是提示：不自动拆分、不自动放款，验收被阻塞
        with self.assertRaises(LedgerError):
            ledger.accept_milestone(event_id=eid(), occurred_at=ts(),
                                    acceptance_id="ACC-X", application_id="APP-1")

    def test_split_then_partial_acceptance_releases_proportion(self):
        ledger = accepted_ledger()
        r1 = ledger.release_tranche(event_id=eid(), occurred_at=ts(), release_id="REL-B1",
                                    contract_id="CT-BANK", application_id="APP-1",
                                    idempotency_key="k-b1")
        r2 = ledger.release_tranche(event_id=eid(), occurred_at=ts(), release_id="REL-S1",
                                    contract_id="CT-SUB", application_id="APP-1",
                                    idempotency_key="k-s1")
        # 100 万设备 × 50% 部分验收 × 50% 拆分比例 = 25 万
        self.assertEqual(r1["event"]["payload"]["amount"], "250000.00")
        self.assertEqual(r2["event"]["payload"]["amount"], "250000.00")
        # 部分验收只释放对应比例：没有剩余可放，重复放款被拒绝
        with self.assertRaises(LedgerError):
            ledger.release_tranche(event_id=eid(), occurred_at=ts(), release_id="REL-BX",
                                   contract_id="CT-BANK", application_id="APP-1",
                                   idempotency_key="k-bx")
        pos = ledger.contract_position("CT-BANK")
        self.assertEqual(pos["released"], Decimal("250000"))
        self.assertEqual(pos["available"], Decimal("350000"))
        self.assertEqual(ledger.verify_conservation(), [])

    def test_reject_blocks_acceptance(self):
        ledger = new_ledger()
        res = submit_equipment(ledger)
        flag_id = res["flags"][0]["payload"]["flag_id"]
        ledger.resolve_overlap(event_id=eid(), occurred_at=ts(), flag_id=flag_id,
                               reviewer_id="REV-1", decision="REJECT", reason="重复申报")
        self.assertEqual(ledger.application_status("APP-1"), "REJECTED")
        with self.assertRaises(LedgerError):
            ledger.accept_milestone(event_id=eid(), occurred_at=ts(),
                                    acceptance_id="ACC-X", application_id="APP-1")

    def test_reviewer_must_be_independent(self):
        ledger = new_ledger()
        res = submit_equipment(ledger)
        flag_id = res["flags"][0]["payload"]["flag_id"]
        with self.assertRaises(LedgerError):  # 申请人本人不能审核
            ledger.resolve_overlap(event_id=eid(), occurred_at=ts(), flag_id=flag_id,
                                   reviewer_id="PM-1", decision="REJECT")
        with self.assertRaises(LedgerError):  # 相关出资方不能审核
            ledger.resolve_overlap(event_id=eid(), occurred_at=ts(), flag_id=flag_id,
                                   reviewer_id="BANK-A", decision="REJECT")
        # 独立审核人可以决定
        ledger.resolve_overlap(event_id=eid(), occurred_at=ts(), flag_id=flag_id,
                               reviewer_id="REV-1", decision="SPLIT",
                               allocation={"CT-BANK": "0.5", "CT-SUB": "0.5"})
        self.assertEqual(ledger.application_status("APP-1"), "RESOLVED")

    def test_split_allocation_validation(self):
        ledger = new_ledger()
        res = submit_equipment(ledger)
        flag_id = res["flags"][0]["payload"]["flag_id"]
        with self.assertRaises(LedgerError):  # 拆分包含未涉及合同
            ledger.resolve_overlap(event_id=eid(), occurred_at=ts(), flag_id=flag_id,
                                   reviewer_id="REV-1", decision="SPLIT",
                                   allocation={"CT-INV": "0.5"})
        with self.assertRaises(LedgerError):  # 拆分比例合计超过 1
            ledger.resolve_overlap(event_id=eid(), occurred_at=ts(), flag_id=flag_id,
                                   reviewer_id="REV-1", decision="SPLIT",
                                   allocation={"CT-BANK": "0.6", "CT-SUB": "0.6"})


class VersionedAdjustmentTest(unittest.TestCase):
    def test_supplementary_invoice_uses_new_version(self):
        ledger = released_ledger()
        ledger.accept_milestone(event_id=eid(), occurred_at=ts(), acceptance_id="ACC-2",
                                application_id="APP-1", accepted_ratio="1",
                                adjusted_claims={"EQUIP": "1100000"}, reason="后补票据 INV-002")
        r = ledger.release_tranche(event_id=eid(), occurred_at=ts(), release_id="REL-B2",
                                   contract_id="CT-BANK", application_id="APP-1",
                                   idempotency_key="k-b2")
        # 追加放款 = 新覆盖 55 万 - 已放 25 万
        self.assertEqual(r["event"]["payload"]["amount"], "300000.00")
        # 原放款事件不被改写
        rel1 = next(e for e in ledger.events
                    if e["kind"] == "TRANCHE_RELEASED"
                    and e["payload"]["release_id"] == "REL-B1")
        self.assertEqual(rel1["payload"]["amount"], "250000.00")
        versions = [e["payload"]["version"] for e in ledger.events
                    if e["kind"] == "MILESTONE_ACCEPTED"]
        self.assertEqual(versions, [1, 2])
        self.assertEqual(ledger.verify_conservation(), [])

    def test_revocation_triggers_recovery_and_conservation(self):
        ledger = released_ledger()
        ledger.accept_milestone(event_id=eid(), occurred_at=ts(), acceptance_id="ACC-2",
                                application_id="APP-1", accepted_ratio="1",
                                adjusted_claims={"EQUIP": "1100000"}, reason="后补票据 INV-002")
        ledger.release_tranche(event_id=eid(), occurred_at=ts(), release_id="REL-B2",
                               contract_id="CT-BANK", application_id="APP-1",
                               idempotency_key="k-b2")
        # 撤销部分结论：覆盖额从 55 万下调到 50 万
        ledger.accept_milestone(event_id=eid(), occurred_at=ts(), acceptance_id="ACC-3",
                                application_id="APP-1", accepted_ratio="1",
                                adjusted_claims={"EQUIP": "1000000"}, reason="撤销部分结论")
        with self.assertRaises(LedgerError):  # 下调后不能再放款
            ledger.release_tranche(event_id=eid(), occurred_at=ts(), release_id="REL-B3",
                                   contract_id="CT-BANK", application_id="APP-1",
                                   idempotency_key="k-b3")
        ledger.reconcile_recovery(event_id=eid(), occurred_at=ts(), recovery_id="REC-B1",
                                  contract_id="CT-BANK", application_id="APP-1",
                                  amount="50000", reason="追回超付")
        pos = ledger.contract_position("CT-BANK")
        self.assertEqual(pos["released"], Decimal("550000"))
        self.assertEqual(pos["recovered"], Decimal("50000"))
        self.assertEqual(pos["available"], Decimal("100000"))
        # 金额守恒：committed == available + released - recovered
        self.assertEqual(pos["committed"],
                         pos["available"] + pos["released"] - pos["recovered"])
        self.assertEqual(ledger.verify_conservation(), [])

    def test_recovery_cannot_exceed_outstanding(self):
        ledger = released_ledger()
        with self.assertRaises(LedgerError):  # 没有超付，不能追偿
            ledger.reconcile_recovery(event_id=eid(), occurred_at=ts(), recovery_id="REC-X",
                                      contract_id="CT-BANK", application_id="APP-1",
                                      amount="1")
        # 验收下调到 40%：覆盖 20 万，已放 25 万，待追偿 5 万
        ledger.accept_milestone(event_id=eid(), occurred_at=ts(), acceptance_id="ACC-2",
                                application_id="APP-1", accepted_ratio="0.4",
                                adjusted_claims={"EQUIP": "1000000"}, reason="撤销部分结论")
        ledger.reconcile_recovery(event_id=eid(), occurred_at=ts(), recovery_id="REC-B1",
                                  contract_id="CT-BANK", application_id="APP-1",
                                  amount="50000")
        with self.assertRaises(LedgerError):  # 超出待追偿余额
            ledger.reconcile_recovery(event_id=eid(), occurred_at=ts(), recovery_id="REC-B2",
                                      contract_id="CT-BANK", application_id="APP-1",
                                      amount="1")
        self.assertEqual(ledger.verify_conservation(), [])


class IdempotencyTest(unittest.TestCase):
    def test_duplicate_receipt_no_double_release(self):
        ledger = accepted_ledger()
        first = ledger.release_tranche(event_id="e-dup-1", occurred_at=ts(),
                                       release_id="REL-B1", contract_id="CT-BANK",
                                       application_id="APP-1", idempotency_key="k-b1")
        self.assertEqual(first["status"], "RECORDED")
        # 同一事件回执重复到达
        again = ledger.release_tranche(event_id="e-dup-1", occurred_at=ts(),
                                       release_id="REL-B1", contract_id="CT-BANK",
                                       application_id="APP-1", idempotency_key="k-b1")
        self.assertEqual(again["status"], "DUPLICATE")
        # 同一幂等键重试（不同事件 id）
        retry = ledger.release_tranche(event_id="e-dup-2", occurred_at=ts(),
                                       release_id="REL-B1b", contract_id="CT-BANK",
                                       application_id="APP-1", idempotency_key="k-b1")
        self.assertEqual(retry["status"], "DUPLICATE")
        releases = [e for e in ledger.events if e["kind"] == "TRANCHE_RELEASED"]
        self.assertEqual(len(releases), 1)
        self.assertEqual(ledger.contract_position("CT-BANK")["released"], Decimal("250000"))

    def test_idempotency_key_cannot_be_reused_by_other_release(self):
        ledger = accepted_ledger()
        ledger.release_tranche(event_id=eid(), occurred_at=ts(), release_id="REL-B1",
                               contract_id="CT-BANK", application_id="APP-1",
                               idempotency_key="k-shared")
        with self.assertRaises(LedgerError):
            ledger.release_tranche(event_id=eid(), occurred_at=ts(), release_id="REL-S1",
                                   contract_id="CT-SUB", application_id="APP-1",
                                   idempotency_key="k-shared")


class ConcurrencyTest(unittest.TestCase):
    def test_concurrent_releases_do_not_overpay(self):
        ledger = accepted_ledger()
        barrier = threading.Barrier(2)
        results, errors = [], []

        def worker(tag):
            barrier.wait(timeout=5)
            try:
                results.append(ledger.release_tranche(
                    event_id=f"e-c-{tag}", occurred_at=ts(), release_id=f"REL-{tag}",
                    contract_id="CT-BANK", application_id="APP-1",
                    idempotency_key=f"k-{tag}"))
            except LedgerError as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in (1, 2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 只有一笔成功，另一笔因无待放款金额被拒绝，不会多付
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(ledger.contract_position("CT-BANK")["released"], Decimal("250000"))
        self.assertEqual(ledger.verify_conservation(), [])

    def test_concurrent_same_idempotency_key(self):
        ledger = accepted_ledger()
        barrier = threading.Barrier(2)
        results = []

        def worker(tag):
            barrier.wait(timeout=5)
            results.append(ledger.release_tranche(
                event_id=f"e-k-{tag}", occurred_at=ts(), release_id=f"REL-K-{tag}",
                contract_id="CT-BANK", application_id="APP-1",
                idempotency_key="k-same"))

        threads = [threading.Thread(target=worker, args=(i,)) for i in (1, 2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(r["status"] for r in results), ["DUPLICATE", "RECORDED"])
        releases = [e for e in ledger.events if e["kind"] == "TRANCHE_RELEASED"]
        self.assertEqual(len(releases), 1)
        self.assertEqual(ledger.contract_position("CT-BANK")["released"], Decimal("250000"))


class ReplayTest(unittest.TestCase):
    def test_replay_restores_state_and_continues(self):
        ledger = full_scenario()
        # 模拟服务中断后恢复：日志中混入重复回执
        replayed = ProgressLedger.replay(ledger.events + ledger.events[:5])
        for cid in ("CT-BANK", "CT-SUB", "CT-INV"):
            self.assertEqual(replayed.contract_position(cid), ledger.contract_position(cid))
        # 幂等键已恢复：重复放款被去重，不会多付
        dup = replayed.release_tranche(event_id=eid(), occurred_at=ts(), release_id="REL-NEW",
                                       contract_id="CT-BANK", application_id="APP-1",
                                       idempotency_key="CT-BANK:APP-1:1")
        self.assertEqual(dup["status"], "DUPLICATE")
        # 恢复后可继续交易，不会漏记
        replayed.submit_application(event_id=eid(), occurred_at=ts(), application_id="APP-9",
                                    project_id="PRJ-1", milestone_id="M3", applicant_id="PM-1",
                                    claims=[{"cost_item_id": "CONTENT", "amount": "100000",
                                             "invoice_refs": ["INV-101"]}])
        replayed.accept_milestone(event_id=eid(), occurred_at=ts(), acceptance_id="ACC-9",
                                  application_id="APP-9", accepted_ratio="1")
        r = replayed.release_tranche(event_id=eid(), occurred_at=ts(), release_id="REL-I9",
                                     contract_id="CT-INV", application_id="APP-9",
                                     idempotency_key="CT-INV:APP-9:1")
        self.assertEqual(r["event"]["payload"]["amount"], "100000.00")
        self.assertEqual(replayed.verify_conservation(), [])


class ViewTest(unittest.TestCase):
    def test_funder_view_is_redacted(self):
        ledger = full_scenario()
        view = ledger.funder_view("BANK-A")
        text = json.dumps(view, ensure_ascii=False)
        self.assertIn("CT-BANK", text)
        # 看不到其他出资方的合同、身份与金额
        for secret in ("CT-SUB", "CT-INV", "FIN-BUREAU", "FUND-C"):
            self.assertNotIn(secret, text)
        kinds = {e["kind"] for e in view}
        self.assertEqual(kinds, {"CONTRACT_COMMITTED", "OVERLAP_FLAGGED",
                                 "OVERLAP_RESOLVED", "TRANCHE_RELEASED",
                                 "RECOVERY_RECONCILED"})
        # 必要的重叠摘要：知道存在重复覆盖与涉及方数量，但不知道对方是谁
        flag = next(e for e in view if e["kind"] == "OVERLAP_FLAGGED")
        self.assertEqual(flag["payload"]["other_party_count"], 1)
        self.assertEqual(flag["payload"]["overlap_amount"], "200000.00")
        resolution = next(e for e in view if e["kind"] == "OVERLAP_RESOLVED")
        self.assertEqual(resolution["payload"]["own_allocation"], {"CT-BANK": "0.5000"})

    def test_project_view_and_regulator_view(self):
        ledger = full_scenario()
        project_events = ledger.project_view("PRJ-1")
        self.assertEqual({e["kind"] for e in project_events}, set(EVENT_KINDS))
        regulator = ledger.regulator_view()
        self.assertEqual(regulator["conservation_problems"], [])
        self.assertEqual(regulator["audit_problems"], [])
        self.assertEqual(regulator["positions"]["CT-BANK"]["available"], "100000.00")
        self.assertEqual(regulator["positions"]["CT-BANK"]["recovered"], "50000.00")

    def test_audit_detects_tampering(self):
        ledger = full_scenario()
        forged = copy.deepcopy(ledger.events)
        for event in forged:
            if event["kind"] == "TRANCHE_RELEASED":
                event["payload"]["amount"] = "1.00"  # 篡改放款金额
                break
        self.assertTrue(ProgressLedger.audit(forged))


class TraceTest(unittest.TestCase):
    def test_trace_release_chain(self):
        ledger = full_scenario()
        trace = ledger.trace_release("REL-B2")
        json.dumps(trace, ensure_ascii=False)  # 项目方可直接序列化使用
        self.assertEqual(trace["release"]["amount"], "300000.00")
        self.assertEqual(trace["release"]["acceptance_id"], "ACC-2")
        self.assertEqual(trace["contract"]["contract_id"], "CT-BANK")
        self.assertEqual(trace["contract"]["funder_kind"], "BANK_LOAN")
        self.assertEqual(trace["application"]["application_id"], "APP-1")
        self.assertEqual(trace["application"]["claims"], {"EQUIP": "1000000.00"})
        # 验收版本链：部分验收 -> 后补票据 -> 撤销结论
        self.assertEqual([a["version"] for a in trace["acceptance_chain"]], [1, 2, 3])
        self.assertEqual(trace["acceptance_chain"][1]["reason"], "后补票据 INV-002")
        # 审批链：重叠提示与审核人拆分决定
        approval = trace["approvals"][0]
        self.assertEqual(approval["resolution"]["decision"], "SPLIT")
        self.assertEqual(approval["resolution"]["reviewer_id"], "REV-1")
        item = trace["cost_breakdown"][0]
        self.assertEqual(item["cost_item_id"], "EQUIP")
        self.assertEqual(item["released_amount"], "300000.00")
        self.assertEqual(item["effective_ratio"], "0.5000")
        self.assertEqual(trace["recoveries"][0]["amount"], "50000.00")


class ConditionTest(unittest.TestCase):
    def test_conditions_gate_release(self):
        ledger = ProgressLedger()
        with self.assertRaises(LedgerError):  # 无法识别的生效条件
            ledger.commit_contract(event_id=eid(), occurred_at=ts(), contract_id="CT-BAD",
                                   project_id="PRJ-1", funder_id="BANK-A",
                                   funder_kind="BANK_LOAN", purpose="x",
                                   cost_items={"PERMIT": {"ratio": "1", "cap": "50000"}},
                                   conditions=["周末放款"], committed_amount="50000")
        ledger.commit_contract(event_id=eid(), occurred_at=ts(), contract_id="CT-X",
                               project_id="PRJ-1", funder_id="BANK-A",
                               funder_kind="BANK_LOAN", purpose="许可办理",
                               cost_items={"PERMIT": {"ratio": "1", "cap": "50000"}},
                               conditions=["milestone:M1"], committed_amount="50000")
        ledger.commit_contract(event_id=eid(), occurred_at=ts(), contract_id="CT-Y",
                               project_id="PRJ-1", funder_id="FUND-C",
                               funder_kind="SOCIAL_INVESTMENT", purpose="内容制作",
                               cost_items={"CONTENT": {"ratio": "1", "cap": "300000"}},
                               conditions=[], committed_amount="300000")
        ledger.submit_application(event_id=eid(), occurred_at=ts(), application_id="APP-M2",
                                  project_id="PRJ-1", milestone_id="M2", applicant_id="PM-1",
                                  claims=[{"cost_item_id": "PERMIT", "amount": "40000",
                                           "invoice_refs": []}])
        ledger.accept_milestone(event_id=eid(), occurred_at=ts(), acceptance_id="ACC-M2",
                                application_id="APP-M2", accepted_ratio="1")
        with self.assertRaises(LedgerError):  # 生效条件未满足：M1 未验收
            ledger.release_tranche(event_id=eid(), occurred_at=ts(), release_id="REL-X1",
                                   contract_id="CT-X", application_id="APP-M2",
                                   idempotency_key="k-x1")
        # 前置里程碑验收后放行
        ledger.submit_application(event_id=eid(), occurred_at=ts(), application_id="APP-M1",
                                  project_id="PRJ-1", milestone_id="M1", applicant_id="PM-1",
                                  claims=[{"cost_item_id": "CONTENT", "amount": "100000",
                                           "invoice_refs": []}])
        ledger.accept_milestone(event_id=eid(), occurred_at=ts(), acceptance_id="ACC-M1",
                                application_id="APP-M1", accepted_ratio="1")
        r = ledger.release_tranche(event_id=eid(), occurred_at=ts(), release_id="REL-X1",
                                   contract_id="CT-X", application_id="APP-M2",
                                   idempotency_key="k-x1")
        self.assertEqual(r["event"]["payload"]["amount"], "40000.00")


class CrossApplicationTest(unittest.TestCase):
    def test_cross_application_reject(self):
        ledger = new_ledger()
        submit_equipment(ledger, "APP-1")
        res2 = ledger.submit_application(
            event_id=eid(), occurred_at=ts(), application_id="APP-2", project_id="PRJ-1",
            milestone_id="M2", applicant_id="PM-1",
            claims=[{"cost_item_id": "EQUIP", "amount": "200000", "invoice_refs": ["INV-009"]}])
        reasons = {f["payload"]["reason"] for f in res2["flags"]}
        self.assertEqual(reasons, {"CROSS_SOURCE", "CROSS_APPLICATION"})
        dup_flag = next(f for f in res2["flags"]
                        if f["payload"]["reason"] == "CROSS_APPLICATION")
        self.assertEqual(dup_flag["payload"]["other_application_ids"], ["APP-1"])
        # 审核人拒绝重复申报，其余未决提示由系统自动关闭
        ledger.resolve_overlap(event_id=eid(), occurred_at=ts(),
                               flag_id=dup_flag["payload"]["flag_id"],
                               reviewer_id="REV-1", decision="REJECT", reason="重复申报")
        self.assertEqual(ledger.application_status("APP-2"), "REJECTED")
        auto = [e for e in ledger.events
                if e["kind"] == "OVERLAP_RESOLVED"
                and e["payload"]["decision"] == "AUTO_CLOSED"]
        self.assertEqual(len(auto), 1)
        with self.assertRaises(LedgerError):
            ledger.accept_milestone(event_id=eid(), occurred_at=ts(),
                                    acceptance_id="ACC-2", application_id="APP-2")

    def test_cross_application_dismiss(self):
        ledger = new_ledger()
        ledger.submit_application(event_id=eid(), occurred_at=ts(), application_id="APP-A",
                                  project_id="PRJ-1", milestone_id="M3", applicant_id="PM-1",
                                  claims=[{"cost_item_id": "CONTENT", "amount": "100000",
                                           "invoice_refs": ["INV-201"]}])
        res = ledger.submit_application(event_id=eid(), occurred_at=ts(),
                                        application_id="APP-B", project_id="PRJ-1",
                                        milestone_id="M4", applicant_id="PM-1",
                                        claims=[{"cost_item_id": "CONTENT", "amount": "50000",
                                                 "invoice_refs": ["INV-202"]}])
        flag = res["flags"][0]["payload"]
        self.assertEqual(flag["reason"], "CROSS_APPLICATION")
        with self.assertRaises(LedgerError):  # 跨申请提示不能拆分
            ledger.resolve_overlap(event_id=eid(), occurred_at=ts(), flag_id=flag["flag_id"],
                                   reviewer_id="REV-1", decision="SPLIT",
                                   allocation={"CT-INV": "1"})
        # 确认为分批制作而非重复，驳回提示后放行
        ledger.resolve_overlap(event_id=eid(), occurred_at=ts(), flag_id=flag["flag_id"],
                               reviewer_id="REV-1", decision="DISMISS", reason="分批制作")
        ledger.accept_milestone(event_id=eid(), occurred_at=ts(), acceptance_id="ACC-B",
                                application_id="APP-B", accepted_ratio="1")
        r = ledger.release_tranche(event_id=eid(), occurred_at=ts(), release_id="REL-B",
                                   contract_id="CT-INV", application_id="APP-B",
                                   idempotency_key="k-inv-b")
        self.assertEqual(r["event"]["payload"]["amount"], "50000.00")


class QuotaTest(unittest.TestCase):
    def test_release_capped_by_available(self):
        ledger = ProgressLedger()
        ledger.commit_contract(event_id=eid(), occurred_at=ts(), contract_id="CT-S",
                               project_id="PRJ-1", funder_id="BANK-A",
                               funder_kind="BANK_LOAN", purpose="设备",
                               cost_items={"EQUIP": {"ratio": "1", "cap": "600000"}},
                               conditions=[], committed_amount="300000")
        ledger.submit_application(event_id=eid(), occurred_at=ts(), application_id="APP-1",
                                  project_id="PRJ-1", milestone_id="M2", applicant_id="PM-1",
                                  claims=[{"cost_item_id": "EQUIP", "amount": "500000",
                                           "invoice_refs": []}])
        ledger.accept_milestone(event_id=eid(), occurred_at=ts(), acceptance_id="ACC-1",
                                application_id="APP-1", accepted_ratio="1")
        with self.assertRaises(LedgerError):  # 覆盖 50 万超过承诺 30 万
            ledger.release_tranche(event_id=eid(), occurred_at=ts(), release_id="REL-1",
                                   contract_id="CT-S", application_id="APP-1",
                                   idempotency_key="k-1")
        # 通过新版本把验收额核减到承诺额度内
        ledger.accept_milestone(event_id=eid(), occurred_at=ts(), acceptance_id="ACC-2",
                                application_id="APP-1", accepted_ratio="1",
                                adjusted_claims={"EQUIP": "250000"}, reason="核减申报")
        r = ledger.release_tranche(event_id=eid(), occurred_at=ts(), release_id="REL-2",
                                   contract_id="CT-S", application_id="APP-1",
                                   idempotency_key="k-2")
        self.assertEqual(r["event"]["payload"]["amount"], "250000.00")
        self.assertEqual(ledger.verify_conservation(), [])


class DomainContractTest(unittest.TestCase):
    def test_events_satisfy_domain_contract(self):
        ledger = full_scenario()
        self.assertTrue(ledger.events)
        for event in ledger.events:
            self.assertEqual(validate_event(event), [], event["event_id"])


if __name__ == "__main__":
    unittest.main()
