# 安置点代理转办的委托范围与授权收回

每条授权有独立编号，功能、地域和有效期均可分别限定。 资料均为本地合成记录。

以 Python 3.11.2 运行，无第三方依赖。`records/` 中的 `delegations`、`holders`、`cases` 对应独立来源。每份材料是 JSON 对象数组，`id` 在同一来源内唯一。报告和观察的时间以各原始记录给出的字段为准。

```sh
python3 -m unittest -v
python3 records_view.py
```

资料入口列出来源、记录标识及条数。通过 `--source` 指定来源，`--offset` 和 `--limit` 限定读取的位置与数量。`--records` 可指定同样结构的其他资料目录。

---

## 委托权限链与转办服务（`delegation_service.py`）

`delegation_service.py` 在只读来源之上提供：代理身份/当前任职核对、权限链逐段收窄核对、
撤回影响、事项接续报告，以及“申请转办 → 签发新依据 → 接手确认 → 重新指派 → 办结/回执”
的完整离线流程。新授权与回执都是**本地合成 JSON**，默认写入 `out/`，不改动 `records/`。

### 规则（代码与 `test_delegation_service.py` 逐条对应）

1. **身份与任职**：`delegations.holder` 对应 `holders.person`；查无此人或其任职记录
   `valid: false` 时不能作为现任代理。点位掌管权来自 holders 记录的 `controls`。
2. **逐段收窄，层数越多权限不得越宽**：子段签发人必须是父段受托人；动作必须同时是
   父段 `actions` 与 `can_pass` 的子集；期限不得晚于父段 `until`；地域必须一致
   （子段不写 `zone` 即继承）；子段确认不得早于上游确认。有效范围取全链交集、
   有效期限取 `min(until)`。
3. **接手确认**：`confirmed_at` 缺失或晚于观察时间的授权不能作为依据；合成新授权在
   受托人 `accept` 之前为 `pending`，不可使用。
4. **一笔办理一条明确依据**：`authorize(..., basis_id=...)` 严格核对持有人、点位、
   动作、时间与全链；同一人若同时握有多条覆盖依据，办理时仍须用 `basis_id` 明确选定。
5. **撤回**：delegations 来源中带 `"for"`/`"at"` 的行是撤回记录（也支持独立
   `withdrawals` 来源）。撤回自 `at`（含）起使目标段及其下游路径失效；不影响其他
   独立授权。撤回前已合法办结的事项（如 `prior-contact`）保持原结论，不改写历史。
6. **时间边界**：整数时间下 `until` 视为含当日（`at <= until` 有效），撤回自生效
   时刻（含）起失效。

### 本资料的核对结论（观察时间 58，即最晚原始事件时间）

| 事项 | 原依据 | 办理当时 | 58 时原依据 | 能否继续 | 现任可办理负责人 |
| --- | --- | --- | --- | --- | --- |
| `case-1`（a / contact，`done:false`，原经办人 r2） | `child-1` → `root-1` | 35 时有效（30/34 均已确认） | 失效：`withdraw-1` 于 50 撤回 `root-1`，整条路径自 50 失效 | r2 凭旧依据**不能**继续；a 点位当前仅 `site-lead-a` 可办理/重新指定 | `site-lead-a` |
| `case-2`（b / room，at 58） | `root-2`（site-lead-b 直接签给 r2，独立于 root-1） | 有效 | **仍然有效**，撤回不及于它 | **能**，依据 `root-2` | `r2`、`site-lead-b` |
| `prior-contact`（a / contact，`done:true`，at 44） | `child-1` | 44 时合法有效，撤回（50）尚未生效 | 当前路径虽已撤回 | 属先前合法完成的事实，保留不改 | — |

要点：r2 不因 `root-1` 被撤回而失去 `root-2`；但 `child-1` 永远只有 `contact`，
不能借转办层数扩张成 `room`。

### 重新派回原经办人

`case-1` 的原经办人就是 r2。派回 r2 时**不复活** `root-1`/`child-1`，而是由掌管 a
点位的 `site-lead-a` 签发一条全新编号、全新期限、仅 `contact` 的直接授权
`grant-case1-r2`，r2 重新确认（回执 `rcpt-accept-r2`），再把事项指派给 r2
（回执 `rcpt-assign-case1`，标记 `basis_is_new` / `back_to_original_handler`）。
此后 `case-1` 在 60 时恢复可继续，新依据为 `grant-case1-r2`。

### 命令用法

```sh
# 一键演完规范情景，生成 out/ 下全部合成授权、申请、指派、回执与两阶段报告
python3 delegation_service.py scenario

# 只读核对（不带 --at 时取资料中最晚事件时间 58；会叠加 out/ 中已合成的状态）
python3 delegation_service.py pending [--at 58] [--open-only]
python3 delegation_service.py report [--at 58] [--case case-1]
python3 delegation_service.py check --basis child-1 --at 58
python3 delegation_service.py authorize --person r2 --zone b --action room --at 58 --basis root-2
python3 delegation_service.py handlers --zone a --action contact --at 58

# 跨进程的转办流程（状态以 JSON 保存在 --out 目录）
python3 delegation_service.py --out work apply-transfer --case case-1 --applicant r2 --at 59
python3 delegation_service.py --out work issue --issuer site-lead-a --holder r2 \
    --zone a --actions contact --until 66 --at 59 --id grant-1 --for-case case-1
python3 delegation_service.py --out work accept --grant grant-1 --holder r2 --at 60
python3 delegation_service.py --out work assign --case case-1 --handler r2 --basis grant-1 --at 60
python3 delegation_service.py --out work complete --case case-1 --person r2 --at 61
```

被拒绝的核对/签发返回 JSON（含原因与权限链缺陷）并以退出码 3 结束；资料错误退出码 2。

### 作为库使用

```python
from pathlib import Path
from delegation_service import (
    DelegationService, build_report, verify_transaction,
    evaluate_basis, eligible_handlers, pending_matters,
)

report = build_report(Path("records"), at=58)
open_cases = pending_matters(Path("records"))                 # case-1
decision = verify_transaction(Path("records"), person="r2",
                              zone="b", action="room", at=58, basis_id="root-2")

service = DelegationService("records", "out")                # 合成状态可落盘续用
service.apply_transfer("case-1", applicant="r2", at=59)
```

### 产物（`out/`，可随时用 `scenario` 重建）

顶层 `out/*.json` 都是带唯一 `id` 的对象数组，可直接用 `records_view.load_sources(Path("out"))` 读取：

- `grants.json`：本地合成的新授权（含新期限与 `confirmed_at` 接手确认）
- `applications.json` / `assignments.json`：转办申请与现任指派链
- `receipts.json`：申请、签发、接手确认、（重新）指派、办结/拒绝回执

`out/reports/` 下是对象形态的报告：

- `report-before.json` / `report-after.json`：处理前（58）与重新指派后（60）的全量报告
  （`report.json` 是处理后最新状态的同名副本）
- `checks.json`：关键授权判定；`scenario-summary.json`：情景结论摘要

测试：`python3 -m unittest -v`（原有来源测试 + `test_delegation_service.py` 的规则测试）。
