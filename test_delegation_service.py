"""委托权限链、撤回影响与转办回执的规则测试。

除直接使用 records/ 中的合成资料外，结构性坏链用例在测试内构造小样本，
全部状态写入临时目录，不依赖仓库下的 out/ 产物。
"""
import json
import tempfile
import unittest
from pathlib import Path

from delegation_service import (
    AuthorizationDenied,
    DelegationError,
    DelegationService,
    HolderDirectory,
    assess_case,
    authorize,
    default_as_of,
    eligible_handlers,
    evaluate_basis,
    run_canonical_scenario,
)

RECORDS = Path(__file__).with_name("records")


class FixtureMixin:
    def setUp(self):
        self.service = DelegationService(RECORDS)
        self.grants = self.service.grants
        self.withdrawals = self.service.withdrawals
        self.holders = self.service.holder_rows = self.service.bundle["holders"]

    def basis(self, basis_id, at):
        return evaluate_basis(
            basis_id, at=at, grants=self.grants,
            withdrawals=self.withdrawals, holders=self.holders)

    def auth(self, **kwargs):
        kwargs.setdefault("grants", self.grants)
        kwargs.setdefault("withdrawals", self.withdrawals)
        kwargs.setdefault("holders", self.holders)
        return authorize(**kwargs)


class ChainShapeTests(FixtureMixin, unittest.TestCase):
    def test_chain_resolves_root_to_leaf(self):
        report = self.basis("child-1", 40)
        self.assertEqual(report["chain"], ["root-1", "child-1"])
        self.assertEqual(report["holder"], "r2")

    def test_effective_scope_is_intersection_and_shortest_term(self):
        report = self.basis("child-1", 40)
        self.assertEqual(report["scope"]["zone"], "a")
        self.assertEqual(report["scope"]["actions"], ["contact"])
        self.assertEqual(report["scope"]["until"], 67)
        self.assertEqual(report["scope"]["confirmed_from"], 34)

    def test_deeper_layer_cannot_widen_to_room(self):
        # 即使在撤回之前，child-1 也只有 contact，room 永远不被覆盖。
        report = self.basis("child-1", 40)
        self.assertTrue(report["valid"])
        decision = self.auth(person="r2", zone="a", action="room",
                             at=40, basis_id="child-1")
        self.assertFalse(decision["authorized"])
        self.assertIn("不覆盖", decision["reason"])

    def test_can_pass_only_lists_contact(self):
        root = self.basis("root-1", 40)
        self.assertEqual(root["scope"]["actions"], ["contact", "room"])
        # root-1 自身可做 room，只是不能转托 room。
        decision = self.auth(person="r1", zone="a", action="room",
                             at=40, basis_id="root-1")
        self.assertTrue(decision["authorized"])


class WithdrawalTests(FixtureMixin, unittest.TestCase):
    def test_path_alive_before_withdrawal_effective_time(self):
        self.assertTrue(self.basis("root-1", 49)["valid"])
        child = self.basis("child-1", 49)
        self.assertTrue(child["valid"])
        self.assertIsNone(child["withdrawn_on_path"])

    def test_path_dead_from_withdrawal_effective_time_inclusive(self):
        for at in (50, 58, 67):
            with self.subTest(at=at):
                root = self.basis("root-1", at)
                child = self.basis("child-1", at)
                self.assertFalse(root["in_force_at"])
                self.assertFalse(child["valid"])
                self.assertEqual(child["withdrawn_on_path"]["id"], "withdraw-1")
                self.assertEqual(child["withdrawn_on_path"]["target"], "root-1")

    def test_withdrawal_does_not_touch_independent_basis(self):
        report = self.basis("root-2", 58)
        self.assertTrue(report["valid"])
        self.assertIsNone(report["withdrawn_on_path"])
        decision = self.auth(person="r2", zone="b", action="room",
                             at=58, basis_id="root-2")
        self.assertTrue(decision["authorized"])

    def test_dead_path_cannot_serve_as_new_transfer_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = DelegationService(RECORDS, tmp)
            with self.assertRaises(AuthorizationDenied):
                service.issue_grant(
                    issuer="r1", holder="r2", zone="a", actions=["contact"],
                    until=66, at=58, parent="root-1")


class TimeAndStandingTests(FixtureMixin, unittest.TestCase):
    def test_confirmation_required_before_use(self):
        report = self.basis("root-1", 29)
        self.assertFalse(report["valid"])
        self.assertTrue(any(i["code"] == "not_yet_confirmed" for i in report["time_issues"]))
        self.assertTrue(self.basis("root-1", 30)["in_force_at"])

    def test_deadline_inclusive(self):
        self.assertTrue(self.basis("root-2", 85)["valid"])
        expired = self.basis("root-2", 86)
        self.assertFalse(expired["valid"])
        self.assertTrue(any(i["code"] == "expired" for i in expired["time_issues"]))

    def test_holder_current_position_required(self):
        holders = [dict(h) for h in self.holders]
        for row in holders:
            if row.get("person") == "r2":
                row["valid"] = False
        report = evaluate_basis(
            "root-2", at=58, grants=self.grants,
            withdrawals=self.withdrawals, holders=holders)
        self.assertFalse(report["standing_ok"])
        self.assertFalse(report["valid"])

    def test_unknown_holder_is_defect(self):
        grants = [dict(g) for g in self.grants]
        grants.append({"id": "g-x", "issuer": "site-lead-b", "zone": "b",
                       "actions": ["room"], "holder": "ghost",
                       "until": 90, "confirmed_at": 50})
        report = evaluate_basis(
            "g-x", at=58, grants=grants,
            withdrawals=self.withdrawals, holders=self.holders)
        self.assertFalse(report["valid"])
        self.assertTrue(any(d["code"] == "holder_unknown" for d in report["defects"]))


class StructuralNarrowingTests(unittest.TestCase):
    def setUp(self):
        self.holders = [
            {"id": "lead-a", "person": "lead", "controls": ["a"]},
            {"id": "s1", "person": "a1", "affiliation": "x", "valid": True},
            {"id": "s2", "person": "a2", "affiliation": "x", "valid": True},
        ]
        self.root = {"id": "r", "issuer": "lead", "zone": "a",
                     "actions": ["contact", "room"], "holder": "a1",
                     "can_pass": ["contact"], "until": 70, "confirmed_at": 10}

    def evaluate(self, child):
        return evaluate_basis(
            "c", at=30, grants=[self.root, child],
            withdrawals=[], holders=self.holders)

    def test_valid_narrow_child(self):
        child = {"id": "c", "parent": "r", "issuer": "a1", "holder": "a2",
                 "actions": ["contact"], "until": 60, "confirmed_at": 20}
        report = self.evaluate(child)
        self.assertTrue(report["valid"], report["defects"])

    def test_action_widening_is_defect(self):
        child = {"id": "c", "parent": "r", "issuer": "a1", "holder": "a2",
                 "actions": ["room"], "until": 60, "confirmed_at": 20}
        report = self.evaluate(child)
        codes = {d["code"] for d in report["defects"]}
        self.assertIn("action_not_delegatable", codes)
        self.assertFalse(report["valid"])

    def test_longer_term_is_defect(self):
        child = {"id": "c", "parent": "r", "issuer": "a1", "holder": "a2",
                 "actions": ["contact"], "until": 71, "confirmed_at": 20}
        report = self.evaluate(child)
        self.assertIn("term_extended", {d["code"] for d in report["defects"]})

    def test_wrong_issuer_is_defect(self):
        child = {"id": "c", "parent": "r", "issuer": "a2", "holder": "a2",
                 "actions": ["contact"], "until": 60, "confirmed_at": 20}
        report = self.evaluate(child)
        self.assertIn("issuer_not_upstream_holder", {d["code"] for d in report["defects"]})

    def test_zone_widening_is_defect(self):
        child = {"id": "c", "parent": "r", "issuer": "a1", "holder": "a2",
                 "zone": "b", "actions": ["contact"], "until": 60, "confirmed_at": 20}
        report = self.evaluate(child)
        self.assertIn("zone_widened", {d["code"] for d in report["defects"]})

    def test_root_issuer_must_control_zone(self):
        rogue = {"id": "x", "issuer": "a1", "zone": "a", "actions": ["contact"],
                 "holder": "a2", "until": 90, "confirmed_at": 10}
        report = evaluate_basis(
            "x", at=30, grants=[rogue], withdrawals=[], holders=self.holders)
        self.assertIn("issuer_controls_zone", {d["code"] for d in report["defects"]})


class EligibleHandlersTests(FixtureMixin, unittest.TestCase):
    def test_before_withdrawal_r2_holds_a_contact_chain(self):
        people = {h["person"] for h in eligible_handlers(
            zone="a", action="contact", at=44,
            grants=self.grants, withdrawals=self.withdrawals, holders=self.holders)}
        self.assertEqual(people, {"r1", "r2", "site-lead-a"})

    def test_after_withdrawal_only_zone_lead_remains_for_a(self):
        rows = eligible_handlers(
            zone="a", action="contact", at=58,
            grants=self.grants, withdrawals=self.withdrawals, holders=self.holders)
        self.assertEqual([h["person"] for h in rows], ["site-lead-a"])
        self.assertEqual(rows[0]["kinds"], ["principal"])

    def test_independent_basis_keeps_r2_handling_b_room(self):
        rows = eligible_handlers(
            zone="b", action="room", at=58,
            grants=self.grants, withdrawals=self.withdrawals, holders=self.holders)
        people = {h["person"] for h in rows}
        self.assertEqual(people, {"r2", "site-lead-b"})
        r2 = next(h for h in rows if h["person"] == "r2")
        self.assertEqual([b["basis"] for b in r2["bases"]], ["root-2"])

    def test_contact_only_agent_not_listed_for_room(self):
        people = {h["person"] for h in eligible_handlers(
            zone="a", action="room", at=44,
            grants=self.grants, withdrawals=self.withdrawals, holders=self.holders)}
        # r1 凭 root-1 可做 room；r2 的 child-1 只有 contact，不在列。
        self.assertEqual(people, {"r1", "site-lead-a"})


class AssessFixtureCasesTests(FixtureMixin, unittest.TestCase):
    def assess(self, case_id, at=58):
        case = next(c for c in self.service.bundle["cases"] if c["id"] == case_id)
        return assess_case(
            case, at=at, grants=self.grants, withdrawals=self.withdrawals,
            holders=self.holders)

    def test_case1_was_validly_taken_but_cannot_continue_on_old_basis(self):
        report = self.assess("case-1")
        self.assertFalse(report["done"])
        self.assertEqual(report["status"], "open")
        self.assertTrue(report["valid_at_taken"])
        self.assertFalse(report["original_basis_currently_valid"])
        self.assertFalse(report["can_continue_now"])
        self.assertEqual(report["responsible_person"], "r2")
        self.assertEqual(report["responsible_persons"], ["site-lead-a"])
        self.assertEqual(report["withdrawal_impact"]["at"], 50)

    def test_case2_continues_via_independent_basis(self):
        report = self.assess("case-2")
        self.assertTrue(report["original_basis_currently_valid"])
        self.assertTrue(report["can_continue_now"])
        self.assertEqual(report["continuation_basis"], "root-2")
        self.assertIn("r2", report["responsible_persons"])

    def test_prior_completion_remains_lawful_fact(self):
        report = self.assess("prior-contact")
        self.assertEqual(report["status"], "done")
        self.assertTrue(report["lawful_completion_preserved"])
        self.assertTrue(report["valid_at_taken"])
        # 撤回影响当前路径，但不改写历史结论。
        self.assertFalse(report["original_basis_currently_valid"])
        hist = report["historical_basis"]
        self.assertTrue(hist["in_force_at"])
        self.assertIsNone(hist["withdrawn_on_path"])

    def test_default_as_of_is_latest_recorded_event(self):
        self.assertEqual(default_as_of(self.service.bundle), 58)

    def test_current_standing_change_does_not_rewrite_lawful_history(self):
        holders = [dict(h) for h in self.holders]
        for row in holders:
            if row.get("person") == "r2":
                row["valid"] = False
        case = next(c for c in self.service.bundle["cases"]
                    if c["id"] == "prior-contact")
        report = assess_case(
            case, at=58, grants=self.grants, withdrawals=self.withdrawals,
            holders=holders)
        # 44 时的链条/时间/撤回判断不依赖“当前任职”字段。
        self.assertTrue(report["valid_at_taken"])
        self.assertTrue(report["historical_basis"]["in_force_at"])
        # 但当前不能再作为合格代理继续办理。
        self.assertFalse(report["original_basis_currently_valid"])


class TransferWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = DelegationService(RECORDS, self.tmp.name)

    def test_full_reassignment_back_to_original_handler(self):
        # 撤回后原经办人 r2 申请转办。
        applied = self.service.apply_transfer(
            "case-1", applicant="r2", at=59, to="r2")
        self.assertFalse(applied["application"]["original_basis_valid_now"])
        self.assertEqual(applied["receipt"]["kind"], "transfer-application")

        # 未确认的新授权不能作为依据。
        grant = self.service.issue_grant(
            issuer="site-lead-a", holder="r2", zone="a",
            actions=["contact"], until=66, at=59,
            grant_id="g-new", for_case="case-1")
        self.assertIsNone(grant["confirmed_at"])
        with self.assertRaises(AuthorizationDenied):
            self.service.assign_case(
                "case-1", handler="r2", basis_id="g-new", at=59)

        # 非指定受托人不能确认。
        with self.assertRaises(AuthorizationDenied):
            self.service.accept_grant("g-new", holder="r1", at=60)

        receipt = self.service.accept_grant("g-new", holder="r2", at=60)
        self.assertEqual(receipt["kind"], "acceptance")
        self.assertTrue(receipt["accepted"])

        result = self.service.assign_case(
            "case-1", handler="r2", basis_id="g-new", at=60)
        self.assertTrue(result["assignment"]["basis_is_new"])
        self.assertEqual(result["receipt"]["kind"], "reassignment")
        self.assertTrue(result["receipt"]["back_to_original_handler"])

        report = self.service.assess(60)
        case1 = next(c for c in report["cases"] if c["id"] == "case-1")
        self.assertTrue(case1["can_continue_now"])
        self.assertEqual(case1["continuation_basis"], "g-new")
        self.assertEqual(case1["current_assignment"]["basis_id"], "g-new")
        self.assertIn("r2", case1["responsible_persons"])

        # 凭新依据可以办结；凭旧 child-1 在 60 年仍被拒绝。
        done = self.service.complete_case("case-1", person="r2", at=61)
        self.assertEqual(done["receipt"]["kind"], "completion")
        with self.assertRaises(AuthorizationDenied):
            self.service.complete_case(
                "case-1", person="r2", at=61, basis_id="child-1")

    def test_new_grant_must_not_revive_revoked_ids(self):
        self.service.issue_grant(
            issuer="site-lead-a", holder="r2", zone="a",
            actions=["contact"], until=66, at=59, grant_id="g-new")
        self.service.accept_grant("g-new", holder="r2", at=60)
        basis = self.service.check_basis("g-new", 60)
        self.assertEqual(basis["chain"], ["g-new"])
        self.assertIsNone(basis["withdrawn_on_path"])
        old = self.service.check_basis("root-1", 60)
        self.assertFalse(old["valid"])

    def test_cannot_issue_beyond_controlled_zone(self):
        with self.assertRaises(AuthorizationDenied):
            self.service.issue_grant(
                issuer="site-lead-a", holder="r2", zone="b",
                actions=["room"], until=90, at=59)

    def test_cannot_issue_to_unknown_person(self):
        with self.assertRaises(DelegationError):
            self.service.issue_grant(
                issuer="site-lead-a", holder="nobody", zone="a",
                actions=["contact"], until=66, at=59)

    def test_multi_hop_new_chain_is_narrowed_and_needs_each_acceptance(self):
        # lead-a -> r1（仅可转托 contact）-> r2（更短期限）
        g1 = self.service.issue_grant(
            issuer="site-lead-a", holder="r1", zone="a",
            actions=["contact", "room"], can_pass=["contact"],
            until=70, at=59, grant_id="g1")
        self.service.accept_grant("g1", holder="r1", at=59)
        g2 = self.service.issue_grant(
            issuer="r1", holder="r2", zone="a", actions=["contact"],
            until=65, at=60, parent="g1", grant_id="g2")
        self.service.accept_grant("g2", holder="r2", at=60)
        report = self.service.check_basis("g2", 61)
        self.assertTrue(report["valid"], report["defects"])
        self.assertEqual(report["scope"]["actions"], ["contact"])
        self.assertEqual(report["scope"]["until"], 65)
        # r1 不能把 room 转出去。
        with self.assertRaises(DelegationError):
            self.service.issue_grant(
                issuer="r1", holder="r2", zone="a", actions=["room"],
                until=65, at=60, parent="g1")

    def test_state_persists_as_local_json_and_reloads(self):
        self.service.issue_grant(
            issuer="site-lead-a", holder="r2", zone="a",
            actions=["contact"], until=66, at=59, grant_id="g-persist")
        self.service.accept_grant("g-persist", holder="r2", at=60)
        files = {p.name for p in Path(self.tmp.name).iterdir()}
        self.assertEqual(files, {"grants.json", "receipts.json"})

        reloaded = DelegationService(RECORDS, self.tmp.name)
        self.assertTrue(reloaded.check_basis("g-persist", 60)["valid"])
        receipts = json.loads((Path(self.tmp.name) / "receipts.json").read_text())
        self.assertTrue(any(r["kind"] == "acceptance" for r in receipts))

    def test_applicant_must_be_responsible_for_case(self):
        with self.assertRaises(AuthorizationDenied):
            self.service.apply_transfer("case-1", applicant="site-lead-b", at=59)


class CanonicalScenarioTests(unittest.TestCase):
    def test_scenario_writes_all_synthesized_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = run_canonical_scenario(RECORDS, tmp)
            root = Path(tmp)
            for name in ("grants.json", "receipts.json", "applications.json",
                         "assignments.json"):
                self.assertTrue((root / name).exists(), name)
            for name in ("report-before.json", "report-after.json",
                         "checks.json", "scenario-summary.json"):
                self.assertTrue((root / "reports" / name).exists(), name)

            self.assertFalse(summary["before"]["case-1"]["can_continue_now"])
            self.assertTrue(summary["before"]["case-2"]["can_continue_now"])
            self.assertTrue(summary["after_case1"]["can_continue_now"])
            self.assertEqual(summary["new_grant_id"], "grant-case1-r2")

            grants = json.loads((root / "grants.json").read_text())
            self.assertEqual(len(grants), 1)
            self.assertIsNotNone(grants[0]["confirmed_at"])
            receipts = json.loads((root / "receipts.json").read_text())
            kinds = [r["kind"] for r in receipts]
            self.assertIn("acceptance", kinds)
            self.assertIn("reassignment", kinds)

    def test_committed_out_artifacts_match_rules(self):
        out = Path(__file__).with_name("out")
        summary_path = out / "reports" / "scenario-summary.json"
        if not summary_path.exists():
            self.skipTest("尚未生成 out/ 合成资料")
        summary = json.loads(summary_path.read_text())
        self.assertTrue(summary["after_case1"]["can_continue_now"])
        self.assertFalse(summary["before"]["case-1"]["can_continue_now"])


class CliTests(unittest.TestCase):
    def test_main_pending_and_scenario_exit_codes(self):
        import io
        import contextlib
        import delegation_service as ds

        with tempfile.TemporaryDirectory() as tmp:
            common = ["--records", str(RECORDS), "--out", tmp]
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = ds.main(common + [
                    "pending", "--open-only", "--at", "58",
                ])
            self.assertEqual(code, 0)
            payload = json.loads(buf.getvalue())
            self.assertEqual([m["id"] for m in payload["matters"]], ["case-1"])

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = ds.main(common + [
                    "authorize", "--person", "r2", "--zone", "b",
                    "--action", "room", "--at", "58", "--basis", "root-2",
                ])
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(buf.getvalue())["authorized"])

            # 越权签发：main 返回 3，不抛异常。
            code = ds.main(common + [
                "issue", "--issuer", "site-lead-a", "--holder", "r2",
                "--zone", "b", "--actions", "room", "--until", "90", "--at", "59",
            ])
            self.assertEqual(code, 3)


if __name__ == "__main__":
    unittest.main()
