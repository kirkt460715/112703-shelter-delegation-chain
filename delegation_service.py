"""委托权限链核对、转办接手与回执的离线服务。

设计要点（对应任务说明）：

* 委托人的点位权限来自 ``holders`` 记录中的 ``controls``；代理身份与当前
  组织任职来自同一来源（``person`` / ``affiliation`` / ``valid``）。
* 每段委托记录可分别限定功能（``actions``）、地域（``zone``，子段可继承）、
  期限（``until``）与可转托事项（``can_pass``），并须有受托人确认
  （``confirmed_at``）。
* 子段权限必须是上游的收窄：动作不得超出上游动作与 ``can_pass``，期限不得
  长于上游，地域必须一致，签发人必须是上游受托人。层数越多权限只能越小。
* 一笔办理必须指定一条覆盖 zone/action/time 全部范围的明确依据。
* 撤回记录（带 ``for``/``at`` 的行）从 ``at``（含）起使目标及其下游路径失效；
* 历史上合法完成的事项只作事实保留，不被改写。
* 转办产生的新授权、接手确认、指派与回执均为本地合成 JSON，可落盘续用。

仅使用 Python 标准库。所有对外报告均为 JSON 可序列化的普通字典/列表。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 允许从任意工作目录、或以 python -I（隐含 -P）按绝对路径启动。
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:  # 兼容从工作区目录或作为包导入两种方式
    from records_view import load_sources
except ImportError:  # pragma: no cover - 仅在包式导入时触发
    from .records_view import load_sources

# 期限边界：整数时间单位下，``until`` 视为截止时刻（含当日/当刻）。
# 撤回自 ``at`` 生效时（含）起失效。
DEADLINE_INCLUSIVE = True

# ---------------------------------------------------------------------------
# 异常类型
# ---------------------------------------------------------------------------


class DelegationError(ValueError):
    """资料或操作不符合规则。"""


class ChainError(DelegationError):
    """权限链缺失、成环或结构不成立。"""


class AuthorizationDenied(DelegationError):
    """在指定时间找不到覆盖全部所需范围的明确依据。"""

    def __init__(self, report):
        self.report = report
        super().__init__(report.get("reason") or "授权依据不成立")


# ---------------------------------------------------------------------------
# 资料装载
# ---------------------------------------------------------------------------


def is_withdrawal(row):
    """撤回行：指向被撤回授权（``for``）且不是一条委托（无 ``holder``）。"""
    return isinstance(row, dict) and "for" in row and "holder" not in row


def load_bundle(records_dir):
    """读取三个独立来源，并把 delegations 中的撤回行拆分出来。

    返回普通字典：``grants`` / ``withdrawals`` / ``holders`` / ``cases`` /
    ``sources``。若资料目录另有 ``withdrawals`` 来源也会合并。
    """
    directory = Path(records_dir)
    sources = load_sources(directory)

    grants, withdrawals = [], []
    for row in sources.get("delegations", []):
        (withdrawals if is_withdrawal(row) else grants).append(dict(row))
    for row in sources.get("withdrawals", []):
        withdrawals.append(dict(row))

    bundle = {
        "directory": directory,
        "sources": sources,
        "grants": grants,
        "withdrawals": withdrawals,
        "holders": [dict(r) for r in sources.get("holders", [])],
        "cases": [dict(r) for r in sources.get("cases", [])],
    }
    validate_bundle(bundle)
    return bundle


def validate_bundle(bundle):
    """交叉核对 id 引用与基本结构，发现明显坏资料立即报错。"""
    grant_ids = [g.get("id") for g in bundle["grants"]]
    if len(grant_ids) != len(set(grant_ids)):
        raise DelegationError("授权编号重复")
    wd_ids = [w.get("id") for w in bundle["withdrawals"] if w.get("id")]
    if len(wd_ids) != len(set(wd_ids)):
        raise DelegationError("撤回记录编号重复")
    for w in bundle["withdrawals"]:
        if not w.get("for"):
            raise DelegationError("撤回记录缺少 for")
        if w["for"] not in grant_ids:
            raise ChainError(f"撤回 {w.get('id')} 指向不存在的授权 {w['for']}")
    return bundle


# ---------------------------------------------------------------------------
# 人员身份 / 组织任职 / 点位掌管权
# ---------------------------------------------------------------------------


class HolderDirectory:
    """holders 来源的查询视图。"""

    def __init__(self, rows):
        # 兼容 holders 行数组、sources 字典（含 "holders" 键）或 person->行 映射。
        if isinstance(rows, HolderDirectory):
            rows = rows.rows
        elif isinstance(rows, dict):
            if isinstance(rows.get("holders"), list):
                rows = rows["holders"]
            elif all(isinstance(v, dict) for v in rows.values()):
                rows = list(rows.values())
            else:
                rows = []
        self.rows = list(rows)
        self._by_person = {}
        for row in self.rows:
            self._by_person.setdefault(row.get("person"), []).append(row)

    def records_for(self, person):
        return list(self._by_person.get(person, []))

    def is_known(self, person):
        return person in self._by_person

    def controlled_zones(self, person):
        zones = set()
        for row in self.records_for(person):
            for zone in row.get("controls", []) or []:
                zones.add(zone)
        return zones

    def has_valid_position(self, person):
        """当前组织任职是否有效。

        * 存在任职/负责记录；
        * 任一记录未被标 ``valid: false``（负责人记录没有 valid 字段时视为有效）。
        """
        rows = self.records_for(person)
        if not rows:
            return False
        return any(row.get("valid", True) is not False for row in rows)

    def position_report(self, person):
        rows = self.records_for(person)
        return {
            "person": person,
            "known": bool(rows),
            "current": self.has_valid_position(person),
            "affiliations": [r.get("affiliation") for r in rows if r.get("affiliation")],
            "controls": sorted(self.controlled_zones(person)),
            "records": [r.get("id") for r in rows],
        }


# ---------------------------------------------------------------------------
# 权限链解析与核对
# ---------------------------------------------------------------------------


def index_grants(grants):
    return {g["id"]: g for g in grants}


def resolve_chain(grant_id, grants_by_id):
    """返回 root -> leaf 的授权记录列表。

    遇到缺失父段或成环时抛 :class:`ChainError`。
    """
    if grant_id not in grants_by_id:
        raise ChainError(f"授权依据不存在: {grant_id}")
    reversed_chain = []
    seen = set()
    current = grant_id
    while current is not None:
        if current in seen:
            raise ChainError(f"授权链成环: {current}")
        if current not in grants_by_id:
            raise ChainError(f"授权链缺少上游段: {current}")
        seen.add(current)
        row = grants_by_id[current]
        reversed_chain.append(row)
        current = row.get("parent")
    return list(reversed(reversed_chain))


def _time_covers(row, at):
    """单段在时间 ``at`` 是否处于已确认且未超期状态。"""
    reasons = []
    confirmed_at = row.get("confirmed_at")
    if confirmed_at is None:
        reasons.append({"code": "unconfirmed", "message": f"{row.get('id')} 尚无接手确认"})
    elif at is not None and confirmed_at > at:
        reasons.append({
            "code": "not_yet_confirmed",
            "message": f"{row.get('id')} 在 {confirmed_at} 才确认，晚于观察时间 {at}",
        })
    until = row.get("until")
    if until is not None and at is not None:
        expired = (at > until) if DEADLINE_INCLUSIVE else (at >= until)
        if expired:
            reasons.append({
                "code": "expired",
                "message": f"{row.get('id')} 期限至 {until}，在 {at} 已失效",
            })
    return reasons


def withdrawal_on_path(chain, withdrawals_by_target, at):
    """找出在 ``at``（含）前已生效、命中链上任一段的最早撤回。

    ``at`` 为 None 表示不做时点判断，此时不套用撤回这一时间效力。
    """
    if at is None:
        return None
    hits = []
    for row in chain:
        for wd in withdrawals_by_target.get(row["id"], []):
            wd_at = wd.get("at")
            if wd_at is not None and wd_at <= at:
                hits.append(wd)
    if not hits:
        return None
    return sorted(hits, key=lambda w: (w.get("at", 0), w.get("id", "")))[0]


def evaluate_basis(grant_id, *, at, grants, withdrawals, holders):
    """完整核对一条授权能否在 ``at`` 作为办理依据。

    返回 JSON 可序列化报告，关键字段：

    ``valid``
        结构无缺陷 + 在有效期内 + 路径未被撤回 + 链上人员当前任职有效。
    ``in_force_at``
        只看确认/期限/撤回（用于评价历史时点的办理事实）。
    ``scope``
        沿全部上游取交集后的实际范围。
    ``covers(...)`` 所需结论直接给在 ``covers_zone`` / ``covers_action``。
    """
    directory = holders if isinstance(holders, HolderDirectory) else HolderDirectory(holders)
    by_id = index_grants(grants)
    withdrawals_by_target = {}
    for wd in withdrawals:
        withdrawals_by_target.setdefault(wd["for"], []).append(wd)

    defects = []
    try:
        chain = resolve_chain(grant_id, by_id)
        chain_ids = [row["id"] for row in chain]
    except ChainError as exc:
        return {
            "basis": grant_id,
            "found": grant_id in by_id,
            "valid": False,
            "in_force_at": False,
            "defects": [{"code": "broken_chain", "message": str(exc)}],
            "chain": [],
            "holder": by_id.get(grant_id, {}).get("holder"),
        }

    root = chain[0]
    effective_zone = root.get("zone")
    effective_actions = set(root.get("actions") or [])
    effective_until = root.get("until")

    # 根段：委托人必须实际掌管该点位。
    root_zone = root.get("zone")
    if root_zone is None:
        defects.append({"code": "missing_zone", "message": f"根授权 {root['id']} 未限定点位"})
    elif root.get("issuer") not in (None,) and root_zone not in directory.controlled_zones(root.get("issuer")):
        defects.append({
            "code": "issuer_controls_zone",
            "message": f"{root.get('issuer')} 不掌管点位 {root_zone}，不能签发 {root['id']}",
        })

    # 逐段收窄核对。
    for parent, child in zip(chain, chain[1:]):
        pid, cid = parent.get("id"), child.get("id")
        if child.get("issuer") != parent.get("holder"):
            defects.append({
                "code": "issuer_not_upstream_holder",
                "message": f"{cid} 的签发人 {child.get('issuer')} 不是 {pid} 的受托人 {parent.get('holder')}",
            })

        child_zone = child.get("zone", parent.get("zone"))
        if child_zone != parent.get("zone"):
            defects.append({
                "code": "zone_widened",
                "message": f"{cid} 点位 {child_zone} 超出上游 {pid} 的 {parent.get('zone')}",
            })

        parent_actions = set(parent.get("actions") or [])
        child_actions = set(child.get("actions") or [])
        can_pass = set(parent.get("can_pass") or [])
        if not child_actions <= parent_actions:
            defects.append({
                "code": "action_widened_vs_upstream",
                "message": f"{cid} 动作 {sorted(child_actions - parent_actions)} 超出 {pid}",
            })
        if not child_actions <= can_pass:
            defects.append({
                "code": "action_not_delegatable",
                "message": f"{pid} 的可转托事项仅 {sorted(can_pass)}，{cid} 却转托 {sorted(child_actions - can_pass)}",
            })

        parent_until = parent.get("until")
        child_until = child.get("until")
        if parent_until is not None and child_until is not None and child_until > parent_until:
            defects.append({
                "code": "term_extended",
                "message": f"{cid} 期限至 {child_until}，晚于上游 {pid} 的 {parent_until}",
            })

        p_conf, c_conf = parent.get("confirmed_at"), child.get("confirmed_at")
        if p_conf is not None and c_conf is not None and c_conf < p_conf:
            defects.append({
                "code": "confirmation_before_upstream",
                "message": f"{cid} 确认于 {c_conf}，早于上游 {pid} 的 {p_conf}",
            })
        # 累积有效范围（交集 + 最短期限 + 统一点位）。
        effective_actions &= child_actions
        if child_until is not None:
            effective_until = child_until if effective_until is None else min(effective_until, child_until)
        if child.get("zone") is not None:
            effective_zone = child["zone"]

    # 每一段自身的“可转托事项”不能超出该段自己拿到的动作。
    for row in chain:
        own_actions = set(row.get("actions") or [])
        can_pass_self = set(row.get("can_pass") or [])
        if not can_pass_self <= own_actions:
            defects.append({
                "code": "can_pass_exceeds_actions",
                "message": f"{row.get('id')} 的可转托事项 {sorted(can_pass_self - own_actions)} 不在其动作范围内",
            })

    # 每一段都必须已确认且在期限内（在观察时点）。
    time_reasons = []
    for row in chain:
        time_reasons.extend(_time_covers(row, at))

    # 当前组织任职：链上每一位受托人都要身份可核、任职有效。
    # 任职是“当前”状态，单独记录，不参与 in_force_at 对历史时点的判断。
    structural_defects = list(defects)
    standing = []
    standing_defects = []
    for row in chain:
        person = row.get("holder")
        report = directory.position_report(person)
        standing.append(report)
        if not report["known"]:
            standing_defects.append({"code": "holder_unknown", "message": f"{row['id']} 受托人 {person} 不在 holders 记录中"})
        elif not report["current"]:
            standing_defects.append({"code": "holder_position_invalid", "message": f"{person} 当前组织任职无效，{row['id']} 不能继续接力"})
    defects.extend(standing_defects)

    wd = withdrawal_on_path(chain, withdrawals_by_target, at)
    withdrawn = wd is not None

    structural_ok = not structural_defects
    in_force = structural_ok and not time_reasons and not withdrawn
    standing_ok = all(item["current"] for item in standing) if standing else False
    valid = in_force and standing_ok

    leaf = chain[-1]
    return {
        "basis": grant_id,
        "found": True,
        "valid": valid,
        "in_force_at": in_force,
        "standing_ok": standing_ok,
        "structural_ok": structural_ok,
        "defects": defects,
        "time_issues": time_reasons,
        "withdrawn_on_path": (
            None if wd is None
            else {"id": wd.get("id"), "target": wd.get("for"), "at": wd.get("at")}
        ),
        "chain": chain_ids,
        "holder": leaf.get("holder"),
        "issuer": root.get("issuer"),
        "scope": {
            "zone": effective_zone,
            "actions": sorted(effective_actions),
            "until": effective_until,
            "confirmed_from": max(
                (r.get("confirmed_at") for r in chain if r.get("confirmed_at") is not None),
                default=None,
            ),
        },
        "standing": standing,
        "at": at,
    }


def basis_covers(report, *, zone, action, at=None):
    """依据报告是否覆盖一笔办理所需的全部范围。"""
    if not report.get("found"):
        return False
    scope = report["scope"]
    if scope.get("zone") != zone:
        return False
    if action not in set(scope.get("actions") or []):
        return False
    if at is not None:
        start = scope.get("confirmed_from")
        until = scope.get("until")
        if start is not None and at < start:
            return False
        if until is not None:
            if DEADLINE_INCLUSIVE and at > until:
                return False
            if not DEADLINE_INCLUSIVE and at >= until:
                return False
    return True


# ---------------------------------------------------------------------------
# 办理授权 / 现任可办理负责人
# ---------------------------------------------------------------------------


def find_bases(*, person, zone, action, at, grants, withdrawals, holders):
    """找出某人在 ``at`` 可作为办理依据的全部完整授权。"""
    result = []
    for grant in grants:
        if grant.get("holder") != person:
            continue
        report = evaluate_basis(
            grant["id"], at=at, grants=grants, withdrawals=withdrawals, holders=holders
        )
        if report["valid"] and basis_covers(report, zone=zone, action=action, at=at):
            result.append(report)
    return sorted(result, key=lambda r: r["basis"])


def authorize(*, person, zone, action, at, grants, withdrawals, holders, basis_id=None):
    """核对一笔办理。

    指定 ``basis_id`` 时只认这一条明确依据；不指定时列出全部可用依据，
    多条依据并存需要办理方明确选择（“一笔办理必须有一条明确依据”）。
    """
    directory = holders if isinstance(holders, HolderDirectory) else HolderDirectory(holders)
    base = {
        "person": person, "zone": zone, "action": action, "at": at,
        "identity": directory.position_report(person),
    }
    if basis_id is not None:
        report = evaluate_basis(
            basis_id, at=at, grants=grants, withdrawals=withdrawals, holders=directory
        )
        covers = basis_covers(report, zone=zone, action=action, at=at)
        authorized = (
            report["valid"]
            and covers
            and report.get("holder") == person
        )
        result = {
            **base,
            "basis_id": basis_id,
            "authorized": authorized,
            "covers_scope": covers,
            "holder_matches": report.get("holder") == person,
            "basis": report,
        }
        if not authorized:
            result["reason"] = _denial_reason(result)
        return result

    bases = find_bases(
        person=person, zone=zone, action=action, at=at,
        grants=grants, withdrawals=withdrawals, holders=directory,
    )
    result = {
        **base,
        # 只要存在至少一条完整覆盖依据，该人即有权限；但若有多条，
        # 具体办理记录仍必须用 basis_id 明确选定一条。
        "authorized": len(bases) >= 1,
        "selection_required": len(bases) > 1,
        "unambiguous": len(bases) == 1,
        "basis_ids": [b["basis"] for b in bases],
        "available_bases": bases,
    }
    if len(bases) == 1:
        result["basis_id"] = bases[0]["basis"]
        result["basis"] = bases[0]
    elif len(bases) > 1:
        result["reason"] = "存在多条覆盖依据，办理时必须用 basis_id 明确指定一条"
        result["ambiguous"] = True
    else:
        result["reason"] = f"{person} 在 {at} 没有覆盖 {zone}/{action} 的有效依据"
    return result


def _denial_reason(decision):
    basis = decision.get("basis") or {}
    if not decision.get("holder_matches", True):
        return f"依据 {decision['basis_id']} 的持有人是 {basis.get('holder')}，不是 {decision['person']}"
    if basis.get("withdrawn_on_path"):
        wd = basis["withdrawn_on_path"]
        return f"依据路径被 {wd['id']} 撤回（{wd['target']} 自 {wd['at']} 起失效）"
    if basis.get("defects"):
        return "权限链不成立: " + "; ".join(d["message"] for d in basis["defects"])
    if basis.get("time_issues"):
        return "; ".join(d["message"] for d in basis["time_issues"])
    if not decision.get("covers_scope", True):
        scope = basis.get("scope") or {}
        return f"依据范围 {scope} 不覆盖 {decision['zone']}/{decision['action']}@{decision['at']}"
    if not basis.get("standing_ok", True):
        return "代理人当前组织任职无效"
    return "授权依据不成立"


def eligible_handlers(*, zone, action, at, grants, withdrawals, holders):
    """列出 ``at`` 时点对该事项可办理的现任负责人/代理人及其依据。

    点位掌管人（holders 中带 ``controls``）依据其掌管权当然在列；
    代理受托人须持有完整有效且覆盖范围的授权链。
    """
    directory = holders if isinstance(holders, HolderDirectory) else HolderDirectory(holders)
    persons = {}

    for row in directory.rows:
        for controlled in row.get("controls", []) or []:
            if controlled == zone and row.get("valid", True) is not False:
                person = row["person"]
                entry = persons.setdefault(person, {
                    "person": person, "kinds": set(), "bases": [],
                })
                entry["kinds"].add("principal")
                entry["bases"].append({
                    "kind": "control", "basis": row["id"], "zones": [zone],
                })

    for person in sorted({g.get("holder") for g in grants if g.get("holder")}):
        bases = find_bases(
            person=person, zone=zone, action=action, at=at,
            grants=grants, withdrawals=withdrawals, holders=directory,
        )
        if not bases:
            continue
        entry = persons.setdefault(person, {
            "person": person, "kinds": set(), "bases": [],
        })
        entry["kinds"].add("agent")
        for report in bases:
            entry["bases"].append({
                "kind": "delegation",
                "basis": report["basis"],
                "chain": report["chain"],
                "until": report["scope"]["until"],
            })

    result = []
    for person in sorted(persons):
        entry = persons[person]
        result.append({
            "person": person,
            "kinds": sorted(entry["kinds"]),
            "bases": sorted(entry["bases"], key=lambda b: b["basis"]),
            "position": directory.position_report(person),
        })
    return result


# ---------------------------------------------------------------------------
# 事项（已领未办）核对
# ---------------------------------------------------------------------------


def case_status(case):
    """done=true 已办结；done=false 已领未办；缺字段视为留痕办理记录。"""
    if case.get("done") is True:
        return "done"
    if case.get("done") is False:
        return "open"
    return "recorded"


def assess_case(case, *, at, grants, withdrawals, holders, assignments=None):
    """生成单个事项的接续核对报告。"""
    directory = holders if isinstance(holders, HolderDirectory) else HolderDirectory(holders)
    basis_id = case.get("taken_using")
    taken_at = case.get("at")
    zone, action = case.get("zone"), case.get("action")

    original = evaluate_basis(
        basis_id, at=at, grants=grants, withdrawals=withdrawals, holders=directory
    ) if basis_id else None
    at_taken = evaluate_basis(
        basis_id, at=taken_at, grants=grants, withdrawals=withdrawals, holders=directory
    ) if basis_id else None

    claimant = (original or {}).get("holder")
    claimant_alt_bases = (
        find_bases(
            person=claimant, zone=zone, action=action, at=at,
            grants=grants, withdrawals=withdrawals, holders=directory,
        )
        if claimant else []
    )

    handlers = eligible_handlers(
        zone=zone, action=action, at=at,
        grants=grants, withdrawals=withdrawals, holders=directory,
    )

    assignment = None
    if assignments:
        candidates = [
            item for item in assignments
            if item.get("case_id") == case["id"]
            and (at is None or item.get("at") is None or item["at"] <= at)
        ]
        if candidates:
            assignment = candidates[-1]

    responsible_person = assignment["handler"] if assignment else claimant
    responsible_decision = None
    if responsible_person:
        chosen_basis = assignment["basis_id"] if assignment else None
        if chosen_basis is None and claimant_alt_bases:
            chosen_basis = claimant_alt_bases[0]["basis"]
        responsible_decision = authorize(
            person=responsible_person, zone=zone, action=action, at=at,
            grants=grants, withdrawals=withdrawals, holders=directory,
            basis_id=chosen_basis,
        )

    status = case_status(case)
    can_continue = bool(responsible_decision and responsible_decision["authorized"])
    return {
        "id": case["id"],
        "zone": zone,
        "action": action,
        "status": status,
        "done": case.get("done"),
        "taken_at": taken_at,
        "original_basis": basis_id,
        "original_chain": (original or {}).get("chain", []),
        "valid_at_taken": bool(
            at_taken and at_taken["in_force_at"]
            and basis_covers(at_taken, zone=zone, action=action, at=taken_at)
            and at_taken.get("holder") is not None
        ),
        "historical_basis": _compact(at_taken),
        "original_basis_currently_valid": bool(original and original["valid"] and basis_covers(
            original, zone=zone, action=action, at=at)),
        "withdrawal_impact": (original or {}).get("withdrawn_on_path"),
        "current_basis_report": _compact(original),
        "claimant": claimant,
        "claimant_other_bases": [b["basis"] for b in claimant_alt_bases
                                 if b["basis"] != basis_id],
        "current_assignment": (
            None if assignment is None
            else {"handler": assignment["handler"], "basis_id": assignment["basis_id"],
                  "at": assignment.get("at"), "receipt_id": assignment.get("receipt_id")}
        ),
        "responsible_person": responsible_person,
        "can_continue_now": can_continue,
        "continuation_basis": (responsible_decision or {}).get("basis_id") if can_continue else None,
        "denial_reason": (responsible_decision or {}).get("reason") if responsible_decision and not can_continue else None,
        "eligible_handlers": handlers,
        "responsible_persons": [h["person"] for h in handlers],
        "principal_persons": [h["person"] for h in handlers if "principal" in h["kinds"]],
        "agent_persons": [h["person"] for h in handlers if "agent" in h["kinds"]],
        "lawful_completion_preserved": status == "done",
        "at": at,
    }


def _compact(report):
    if not report:
        return None
    return {
        "basis": report.get("basis"),
        "valid": report.get("valid"),
        "in_force_at": report.get("in_force_at"),
        "chain": report.get("chain"),
        "scope": report.get("scope"),
        "withdrawn_on_path": report.get("withdrawn_on_path"),
        "defects": report.get("defects"),
        "time_issues": report.get("time_issues"),
    }


def default_as_of(bundle):
    """资料中最晚的事件时间（cases.at / withdrawals.at），不含期限字段。"""
    values = [c.get("at") for c in bundle["cases"] if c.get("at") is not None]
    values += [w.get("at") for w in bundle["withdrawals"] if w.get("at") is not None]
    return max(values) if values else None


# ---------------------------------------------------------------------------
# 目录级便捷入口（无合成状态，只读原始 records）
# ---------------------------------------------------------------------------


def build_report(records_dir, at=None):
    """读取资料目录并输出全部事项的接续核对报告。"""
    return DelegationService(records_dir).assess(at)


def pending_matters(records_dir, at=None):
    """已领未办（done is False）事项的核对报告列表。"""
    report = build_report(records_dir, at)
    return [c for c in report["cases"] if c["status"] == "open"]


def pending_summary(records_dir, at=None, *, open_only=False):
    """目录级“原依据 / 当前能否继续 / 现任可办理人”精简清单。"""
    return DelegationService(records_dir).pending_summary(at, open_only=open_only)


def verify_transaction(records_dir, *, person, zone, action, at, basis_id=None):
    """目录级一笔办理核对入口。"""
    bundle = load_bundle(records_dir)
    return authorize(
        person=person, zone=zone, action=action, at=at, basis_id=basis_id,
        grants=bundle["grants"], withdrawals=bundle["withdrawals"],
        holders=bundle["holders"],
    )


def verify_basis_record(records_dir, basis_id, at):
    """目录级单条依据链核对入口。"""
    bundle = load_bundle(records_dir)
    return evaluate_basis(
        basis_id, at=at, grants=bundle["grants"],
        withdrawals=bundle["withdrawals"], holders=bundle["holders"],
    )


# ---------------------------------------------------------------------------
# 本地合成状态：新授权 / 申请 / 指派 / 回执，JSON 落盘
# ---------------------------------------------------------------------------


STATE_FILES = {
    "grants": "grants.json",
    "applications": "applications.json",
    "assignments": "assignments.json",
    "receipts": "receipts.json",
    "completions": "completions.json",
}


def _next_numeric_id(prefix, existing):
    top = 0
    for value in existing:
        if isinstance(value, str) and value.startswith(prefix + "-"):
            tail = value[len(prefix) + 1:]
            if tail.isdigit():
                top = max(top, int(tail))
    return f"{prefix}-{top + 1}"


class DelegationService:
    """对外的经办服务：核对 + 申请转办 + 签发/接受新依据 + 指派 + 回执。

    合成状态保存在 ``out_dir`` 的 JSON 文件中；``out_dir`` 为 None 时仅内存。
    基础资料目录 ``records_dir`` 保持只读。
    """

    def __init__(self, records_dir, out_dir=None):
        self.bundle = load_bundle(records_dir)
        self.directory = HolderDirectory(self.bundle["holders"])
        self.out_dir = Path(out_dir) if out_dir else None
        self.synthetic = {key: [] for key in STATE_FILES}
        if self.out_dir:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            for key, filename in STATE_FILES.items():
                path = self.out_dir / filename
                if path.exists():
                    rows = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(rows, list):
                        raise DelegationError(f"{path} 应为对象数组")
                    self.synthetic[key] = rows

    # -- 视图 -------------------------------------------------------------

    @property
    def grants(self):
        return self.bundle["grants"] + self.synthetic["grants"]

    @property
    def withdrawals(self):
        return self.bundle["withdrawals"]

    def _save(self, key):
        if not self.out_dir:
            return
        path = self.out_dir / STATE_FILES[key]
        path.write_text(
            json.dumps(self.synthetic[key], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _receipt(self, kind, *, at, actor, ok=True, receipt_id=None, **fields):
        rid = receipt_id or _next_numeric_id("rcpt", [r["id"] for r in self.synthetic["receipts"]])
        if any(r["id"] == rid for r in self.synthetic["receipts"]):
            raise DelegationError(f"回执编号重复: {rid}")
        receipt = {"id": rid, "kind": kind, "ok": ok, "at": at, "actor": actor, **fields}
        self.synthetic["receipts"].append(receipt)
        self._save("receipts")
        return receipt

    # -- 只读核对 ----------------------------------------------------------

    def check_basis(self, basis_id, at):
        return evaluate_basis(
            basis_id, at=at, grants=self.grants,
            withdrawals=self.withdrawals, holders=self.directory,
        )

    def authorize(self, *, person, zone, action, at, basis_id=None):
        return authorize(
            person=person, zone=zone, action=action, at=at, basis_id=basis_id,
            grants=self.grants, withdrawals=self.withdrawals, holders=self.directory,
        )

    def handlers(self, *, zone, action, at):
        return eligible_handlers(
            zone=zone, action=action, at=at,
            grants=self.grants, withdrawals=self.withdrawals, holders=self.directory,
        )

    def assess(self, at=None):
        if at is None:
            at = default_as_of(self.bundle)
        assignments = self.synthetic["assignments"]
        cases = [
            assess_case(c, at=at, grants=self.grants,
                        withdrawals=self.withdrawals, holders=self.directory,
                        assignments=assignments)
            for c in self.bundle["cases"]
        ]
        return {
            "as_of": at,
            "cases": cases,
            # done is False：明确“已领未办”；done 非 true 都属未结/可核对接续。
            "open_case_ids": [c["id"] for c in cases if c["status"] == "open"],
            "unfinished_case_ids": [c["id"] for c in cases
                                    if not c["lawful_completion_preserved"]],
            "continuable_now_ids": [c["id"] for c in cases if c["can_continue_now"]],
        }

    def get_case(self, case_id):
        for case in self.bundle["cases"]:
            if case["id"] == case_id:
                return case
        raise DelegationError(f"事项不存在: {case_id}")

    def pending_summary(self, at=None, *, open_only=False):
        """已领未办/未结事项的精简视图：原依据、能否继续、现任可办理人。"""
        report = self.assess(at)
        matters = []
        for case in report["cases"]:
            if open_only and case["status"] != "open":
                continue
            if not open_only and case["status"] == "done":
                continue
            matters.append({
                "id": case["id"],
                "status": case["status"],
                "zone": case["zone"],
                "action": case["action"],
                "original_basis": case["original_basis"],
                "original_basis_currently_valid": case["original_basis_currently_valid"],
                "withdrawal_impact": case["withdrawal_impact"],
                "current_assignment": case["current_assignment"],
                "responsible_person": case["responsible_person"],
                "can_continue_now": case["can_continue_now"],
                "continuation_basis": case["continuation_basis"],
                "denial_reason": case["denial_reason"],
                "responsible_persons": case["responsible_persons"],
            })
        return {"as_of": report["as_of"], "matters": matters}

    # -- 经办申请转办 -------------------------------------------------------

    def apply_transfer(self, case_id, *, applicant, at, reason=None, to=None,
                       application_id=None, receipt_id=None):
        """经办人提交转办申请。

        即使原依据已被撤回，原经办人仍可提出申请——申请本身是请求而非行权。
        """
        case = self.get_case(case_id)
        original = evaluate_basis(
            case.get("taken_using"), at=at, grants=self.grants,
            withdrawals=self.withdrawals, holders=self.directory,
        ) if case.get("taken_using") else None
        claimant = (original or {}).get("holder")
        current = self._current_assignment(case_id)
        responsible = current["handler"] if current else claimant
        if applicant != responsible and applicant != claimant:
            raise AuthorizationDenied({
                "reason": f"{applicant} 不是 {case_id} 的经办/现任负责人（{responsible}）",
            })
        app_id = application_id or _next_numeric_id(
            "app", [a["id"] for a in self.synthetic["applications"]])
        application = {
            "id": app_id,
            "case_id": case_id,
            "applicant": applicant,
            "at": at,
            "to": to,
            "reason": reason,
            "original_basis": case.get("taken_using"),
            "original_basis_valid_now": bool(original and original["valid"]),
            "withdrawal_impact": (original or {}).get("withdrawn_on_path"),
            "status": "requested",
        }
        self.synthetic["applications"].append(application)
        self._save("applications")
        receipt = self._receipt(
            "transfer-application", at=at, actor=applicant,
            receipt_id=receipt_id, application_id=app_id, case_id=case_id, to=to,
        )
        return {"application": application, "receipt": receipt}

    def _current_assignment(self, case_id):
        for item in reversed(self.synthetic["assignments"]):
            if item.get("case_id") == case_id and item.get("current", True):
                return item
        return None

    # -- 签发新授权（指定接手人） -------------------------------------------

    def issue_grant(self, *, issuer, holder, zone, actions, until, at,
                    parent=None, can_pass=None, grant_id=None,
                    replaces=None, for_case=None, note=None):
        """签发一段新授权（此时尚未经受托人确认，暂不可用）。

        根授权要求签发人掌管该点位；转托段要求上游依据在 ``at`` 有效，且
        动作/期限/地域全面收窄。被撤回的上游不能再用于签发新段。
        """
        actions = sorted(set(actions or []))
        if not actions:
            raise DelegationError("新授权至少要包含一项动作")
        can_pass = sorted(set(can_pass or []))
        if not set(can_pass) <= set(actions):
            raise DelegationError("可转托事项必须是本授权动作的子集")
        if until is None:
            raise DelegationError("新授权必须明确期限 until")

        grant = {
            "id": grant_id or _next_numeric_id("grant", [g["id"] for g in self.grants]),
            "issuer": issuer,
            "holder": holder,
            "zone": zone,
            "actions": actions,
            "can_pass": can_pass,
            "until": until,
            "issued_at": at,
            "confirmed_at": None,
            "status": "pending",
        }
        if parent is not None:
            grant["parent"] = parent
            grant.pop("zone")  # 子段默认继承上游；如显式需要可在下方写回
            report = self.check_basis(parent, at)
            if not report["valid"]:
                raise AuthorizationDenied({
                    "reason": f"上游依据 {parent} 在 {at} 不成立，不能据以转办",
                    "basis": report,
                })
            if report["holder"] != issuer:
                raise AuthorizationDenied({
                    "reason": f"{issuer} 不是上游 {parent} 的受托人 {report['holder']}",
                })
            scope = report["scope"]
            if scope["zone"] != zone:
                raise DelegationError(f"转托点位 {zone} 与上游 {scope['zone']} 不一致")
            if not set(actions) <= set(scope["actions"]):
                raise DelegationError("转托动作超出上游有效动作范围")
            upstream = index_grants(self.grants)[parent]
            if not set(actions) <= set(upstream.get("can_pass") or []):
                raise DelegationError(f"上游 {parent} 未授权转托这些动作")
            if scope["until"] is not None and until > scope["until"]:
                raise DelegationError("转托期限不得晚于上游最短期限")
            grant["zone"] = zone
        else:
            if zone not in self.directory.controlled_zones(issuer):
                raise AuthorizationDenied({
                    "reason": f"{issuer} 不掌管点位 {zone}，无权签发独立授权",
                })

        if not self.directory.is_known(holder):
            raise DelegationError(f"受托人 {holder} 身份不在 holders 记录中")

        if replaces is not None:
            grant["replaces"] = replaces
        if for_case is not None:
            grant["new_basis_for"] = for_case
        if note is not None:
            grant["note"] = note

        if any(g["id"] == grant["id"] for g in self.grants):
            raise DelegationError(f"授权编号重复: {grant['id']}")
        self.synthetic["grants"].append(grant)
        self._save("grants")
        self._receipt(
            "grant-issued", at=at, actor=issuer, grant_id=grant["id"],
            holder=holder, case_id=for_case,
        )
        return grant

    # -- 接手确认 ----------------------------------------------------------

    def accept_grant(self, grant_id, *, holder, at, receipt_id=None):
        """受托人确认接手；确认后授权才成为可用依据，并给回执。"""
        grant = self._synthetic_grant(grant_id)
        if grant.get("holder") != holder:
            raise AuthorizationDenied({"reason": f"{holder} 不是 {grant_id} 的指定受托人"})
        if grant.get("confirmed_at") is not None:
            raise DelegationError(f"{grant_id} 已经在 {grant['confirmed_at']} 确认过")
        position = self.directory.position_report(holder)
        if not position["current"]:
            raise AuthorizationDenied({"reason": f"{holder} 当前组织任职无效，不能确认接手"})
        if at < grant.get("issued_at", at):
            raise DelegationError("确认时间不能早于签发时间")
        if grant.get("until") is not None and at > grant["until"]:
            raise DelegationError("期限已过，不能再确认接手")

        grant["confirmed_at"] = at
        grant["status"] = "confirmed"
        self._save("grants")
        return self._receipt(
            "acceptance", at=at, actor=holder, receipt_id=receipt_id,
            grant_id=grant_id, accepted=True,
        )

    def _synthetic_grant(self, grant_id):
        for grant in self.synthetic["grants"]:
            if grant["id"] == grant_id:
                return grant
        raise DelegationError(f"不是本服务签发的待确认授权: {grant_id}")

    # -- 重新指派事项 -------------------------------------------------------

    def assign_case(self, case_id, *, handler, basis_id, at,
                    assignment_id=None, receipt_id=None):
        """把事项的现任办理人记为 ``handler``，并指定明确依据 ``basis_id``。

        依据必须在 ``at`` 完整有效、持有人正是 handler、覆盖事项的 zone/action。
        派回原经办人同样要求一条新的有效依据（旧被撤回路径不能复活）。
        """
        case = self.get_case(case_id)
        decision = self.authorize(
            person=handler, zone=case["zone"], action=case["action"],
            at=at, basis_id=basis_id,
        )
        if not decision["authorized"]:
            raise AuthorizationDenied(decision)

        previous = self._current_assignment(case_id)
        if previous:
            previous["current"] = False
        assignment_id = assignment_id or _next_numeric_id(
            "asgn", [a["id"] for a in self.synthetic["assignments"]])
        assignment = {
            "id": assignment_id,
            "case_id": case_id,
            "handler": handler,
            "basis_id": basis_id,
            "at": at,
            "current": True,
            "previous_assignment": (previous or {}).get("id"),
            "original_basis": case.get("taken_using"),
            "basis_is_new": basis_id != case.get("taken_using"),
        }
        self.synthetic["assignments"].append(assignment)
        self._save("assignments")
        is_reassignment = bool(previous or case.get("taken_using"))
        receipt = self._receipt(
            "reassignment" if is_reassignment else "assignment",
            at=at, actor=handler, receipt_id=receipt_id,
            case_id=case_id, handler=handler, basis_id=basis_id,
            basis_is_new=assignment["basis_is_new"],
            back_to_original_handler=(handler == (
                evaluate_basis(
                    case.get("taken_using"), at=at, grants=self.grants,
                    withdrawals=self.withdrawals, holders=self.directory,
                ).get("holder") if case.get("taken_using") else None)),
        )
        assignment["receipt_id"] = receipt["id"]
        self._save("assignments")
        return {"assignment": assignment, "receipt": receipt, "decision": decision}

    # -- 办结 --------------------------------------------------------------

    def complete_case(self, case_id, *, person, at, basis_id=None, receipt_id=None):
        """在明确依据下办结一笔；拒绝时抛出带核对报告的异常。"""
        case = self.get_case(case_id)
        if basis_id is None:
            current = self._current_assignment(case_id)
            basis_id = current["basis_id"] if current else case.get("taken_using")
        decision = self.authorize(
            person=person, zone=case["zone"], action=case["action"],
            at=at, basis_id=basis_id,
        )
        if not decision["authorized"]:
            denied = self._receipt(
                "completion-denied", at=at, actor=person, ok=False,
                case_id=case_id, basis_id=basis_id, reason=decision.get("reason"),
            )
            raise AuthorizationDenied({**decision, "denial_receipt": denied})

        completion = {
            "case_id": case_id, "person": person, "basis_id": basis_id, "at": at,
        }
        self.synthetic["completions"].append(completion)
        self._save("completions")
        receipt = self._receipt(
            "completion", at=at, actor=person, receipt_id=receipt_id,
            case_id=case_id, basis_id=basis_id,
        )
        return {"completion": completion, "receipt": receipt, "decision": decision}

    def reset_state(self):
        """清空并删除本服务管理的合成状态文件（不碰 records 原始资料）。"""
        self.synthetic = {key: [] for key in STATE_FILES}
        if self.out_dir:
            for filename in STATE_FILES.values():
                path = self.out_dir / filename
                if path.exists():
                    path.unlink()


# ---------------------------------------------------------------------------
# 规范情景：核对撤回影响 -> 申请转办 -> 新授权 -> 接手确认 -> 派回原经办人
# ---------------------------------------------------------------------------


def run_canonical_scenario(records_dir, out_dir=None, *, as_of=58, action_at=59,
                           accept_at=60, assign_at=60, grant_until=66):
    """按资料中的人物关系演一遍任务描述的完整流程，并落盘全部回执。

    时间取值刻意避开边界：撤回生效于 50，最新原始观察为 case-2 的 58。
    ``out_dir`` 默认为本模块旁的 ``out/``。
    """
    if out_dir is None:
        out_dir = Path(__file__).with_name("out")
    service = DelegationService(records_dir, out_dir)
    service.reset_state()
    reports_dir = service.out_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    before = service.assess(as_of)
    (reports_dir / "report-before.json").write_text(
        json.dumps(before, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    checks = {
        "as_of": as_of,
        "case1_original_path": service.check_basis("child-1", as_of),
        "case2_independent_basis": service.authorize(
            person="r2", zone="b", action="room", at=as_of, basis_id="root-2"),
        "r2_a_contact_without_new_grant": service.authorize(
            person="r2", zone="a", action="contact", at=as_of),
        "r2_a_room_cannot_widen": service.authorize(
            person="r2", zone="a", action="room", at=40, basis_id="child-1"),
        "prior_contact_was_lawful_at_44": service.authorize(
            person="r2", zone="a", action="contact", at=44, basis_id="child-1"),
    }
    (reports_dir / "checks.json").write_text(
        json.dumps(checks, ensure_ascii=False, indent=2, default=list) + "\n",
        encoding="utf-8")

    # r2 是 case-1 的原经办人，撤回后申请转办/重新指定。
    application = service.apply_transfer(
        "case-1", applicant="r2", at=action_at,
        reason="root-1 被撤回，child-1 路径失效，申请重新指定",
        to="r2", application_id="app-case1-2026",
    )

    # a 点位掌管人 site-lead-a 签发一条全新的、仅 contact 的直接授权给 r2。
    grant = service.issue_grant(
        issuer="site-lead-a", holder="r2", zone="a", actions=["contact"],
        can_pass=[], until=grant_until, at=action_at,
        grant_id="grant-case1-r2", replaces="child-1", for_case="case-1",
        note="撤回后重新指定；新编号、新期限，不复活 root-1/child-1",
    )
    acceptance = service.accept_grant(
        grant["id"], holder="r2", at=accept_at, receipt_id="rcpt-accept-r2")
    assignment = service.assign_case(
        "case-1", handler="r2", basis_id=grant["id"], at=assign_at,
        assignment_id="asgn-case1-current", receipt_id="rcpt-assign-case1",
    )

    after = service.assess(max(as_of, assign_at))
    (reports_dir / "report-after.json").write_text(
        json.dumps(after, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # report.json 作为“处理后最新状态”的常规入口，内容与 report-after 相同。
    (reports_dir / "report.json").write_text(
        json.dumps(after, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    summary = {
        "before": {c["id"]: {
            "status": c["status"],
            "original_basis": c["original_basis"],
            "valid_at_taken": c["valid_at_taken"],
            "original_basis_currently_valid": c["original_basis_currently_valid"],
            "can_continue_now": c["can_continue_now"],
            "responsible_person": c["responsible_person"],
            "responsible_persons": c["responsible_persons"],
            "withdrawal_impact": c["withdrawal_impact"],
        } for c in before["cases"]},
        "application_id": application["application"]["id"],
        "new_grant_id": grant["id"],
        "acceptance_receipt": acceptance["id"],
        "assignment_receipt": assignment["receipt"]["id"],
        "after_case1": next(c for c in after["cases"] if c["id"] == "case-1"),
        "artifacts": sorted(
            str(p.relative_to(service.out_dir))
            for p in service.out_dir.rglob("*") if p.is_file()),
    }
    (reports_dir / "scenario-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path, default=Path(__file__).with_name("records"))
    parser.add_argument("--out", type=Path, default=Path(__file__).with_name("out"))
    sub = parser.add_subparsers(dest="command")

    p_report = sub.add_parser("report", help="输出全部事项的接续核对报告")
    p_report.add_argument("--at", type=int)
    p_report.add_argument("--case")

    p_pending = sub.add_parser("pending", help="未结事项精简视图：原依据/能否继续/现任负责人")
    p_pending.add_argument("--at", type=int)
    p_pending.add_argument("--open-only", action="store_true",
                           help="只列 done=false 的已领未办事项")

    p_check = sub.add_parser("check", help="核对一条依据的完整权限链")
    p_check.add_argument("--basis", required=True)
    p_check.add_argument("--at", type=int, required=True)

    p_auth = sub.add_parser("authorize", help="核对一笔办理是否有明确完整依据")
    p_auth.add_argument("--person", required=True)
    p_auth.add_argument("--zone", required=True)
    p_auth.add_argument("--action", required=True)
    p_auth.add_argument("--at", type=int, required=True)
    p_auth.add_argument("--basis")

    p_handlers = sub.add_parser("handlers", help="列出当前可办理某事项的负责人")
    p_handlers.add_argument("--zone", required=True)
    p_handlers.add_argument("--action", required=True)
    p_handlers.add_argument("--at", type=int, required=True)

    sub.add_parser("scenario", help="运行规范转办情景并生成 out/ 下的授权与回执")

    p_apply = sub.add_parser("apply-transfer")
    p_apply.add_argument("--case", required=True)
    p_apply.add_argument("--applicant", required=True)
    p_apply.add_argument("--at", type=int, required=True)
    p_apply.add_argument("--to")
    p_apply.add_argument("--reason")

    p_issue = sub.add_parser("issue")
    p_issue.add_argument("--issuer", required=True)
    p_issue.add_argument("--holder", required=True)
    p_issue.add_argument("--zone", required=True)
    p_issue.add_argument("--actions", required=True, help="逗号分隔")
    p_issue.add_argument("--until", type=int, required=True)
    p_issue.add_argument("--at", type=int, required=True)
    p_issue.add_argument("--parent")
    p_issue.add_argument("--can-pass", default="")
    p_issue.add_argument("--id", dest="grant_id")
    p_issue.add_argument("--for-case")
    p_issue.add_argument("--replaces")

    p_accept = sub.add_parser("accept")
    p_accept.add_argument("--grant", required=True)
    p_accept.add_argument("--holder", required=True)
    p_accept.add_argument("--at", type=int, required=True)

    p_assign = sub.add_parser("assign")
    p_assign.add_argument("--case", required=True)
    p_assign.add_argument("--handler", required=True)
    p_assign.add_argument("--basis", required=True)
    p_assign.add_argument("--at", type=int, required=True)

    p_complete = sub.add_parser("complete")
    p_complete.add_argument("--case", required=True)
    p_complete.add_argument("--person", required=True)
    p_complete.add_argument("--at", type=int, required=True)
    p_complete.add_argument("--basis")

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "scenario":
            summary = run_canonical_scenario(args.records, args.out)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0

        service = DelegationService(args.records, args.out)

        if args.command in (None, "report"):
            report = service.assess(getattr(args, "at", None))
            case_id = getattr(args, "case", None)
            if case_id:
                report = next((c for c in report["cases"] if c["id"] == case_id), None)
                if report is None:
                    raise DelegationError(f"事项不存在: {case_id}")
        elif args.command == "pending":
            report = service.pending_summary(args.at, open_only=args.open_only)
        elif args.command == "check":
            report = service.check_basis(args.basis, args.at)
        elif args.command == "authorize":
            report = service.authorize(
                person=args.person, zone=args.zone, action=args.action,
                at=args.at, basis_id=args.basis)
        elif args.command == "handlers":
            report = service.handlers(zone=args.zone, action=args.action, at=args.at)
        elif args.command == "apply-transfer":
            report = service.apply_transfer(
                args.case, applicant=args.applicant, at=args.at,
                reason=args.reason, to=args.to)
        elif args.command == "issue":
            report = service.issue_grant(
                issuer=args.issuer, holder=args.holder, zone=args.zone,
                actions=[a for a in args.actions.split(",") if a],
                until=args.until, at=args.at, parent=args.parent,
                can_pass=[a for a in args.can_pass.split(",") if a],
                grant_id=args.grant_id, replaces=args.replaces, for_case=args.for_case)
        elif args.command == "accept":
            report = service.accept_grant(args.grant, holder=args.holder, at=args.at)
        elif args.command == "assign":
            report = service.assign_case(
                args.case, handler=args.handler, basis_id=args.basis, at=args.at)
        elif args.command == "complete":
            report = service.complete_case(
                args.case, person=args.person, at=args.at, basis_id=args.basis)
        else:
            parser.print_help()
            return 1
    except AuthorizationDenied as exc:
        print(json.dumps({"authorized": False, **exc.report}, ensure_ascii=False, indent=2,
                         default=list), flush=True)
        return 3
    except (DelegationError, OSError) as exc:
        parser.error(str(exc))
        return 2

    print(json.dumps(report, ensure_ascii=False, indent=2, default=list))
    return 0


# ---------------------------------------------------------------------------
# 常见命名别名，方便按不同命名习惯调用同一套核对逻辑
# ---------------------------------------------------------------------------

verify_delegation = evaluate_basis
check_chain = evaluate_basis
verify_basis = evaluate_basis
verify_agent = HolderDirectory
assess_matters = assess_case
pending_handlers = eligible_handlers
AgencyService = DelegationService
Service = DelegationService


if __name__ == "__main__":
    raise SystemExit(main())
