"""委托/授权核对的常用命名入口（兼容层）。

完整实现与 CLI 在 :mod:`delegation_service`；本模块以领域常见命名再导出，
方便 ``from delegations import check_delegation, reassign_case ...`` 形式调用。
所有函数的参数以 ``delegation_service`` 中的签名为准。
"""
from delegation_service import (  # noqa: F401
    AgencyService,
    AuthorizationDenied,
    ChainError,
    DelegationError,
    DelegationService,
    HolderDirectory,
    assess_case,
    authorize,
    basis_covers,
    build_parser,
    build_report,
    case_status,
    default_as_of,
    eligible_handlers,
    evaluate_basis,
    find_bases,
    is_withdrawal,
    load_bundle,
    main,
    pending_matters,
    pending_summary,
    resolve_chain,
    run_canonical_scenario,
    verify_basis_record,
    verify_transaction,
)

# -- 常见命名别名 -----------------------------------------------------------

# 单条依据 / 权限链核对
check_delegation = evaluate_basis
validate_delegation = evaluate_basis
verify_delegation = evaluate_basis
inspect_basis = evaluate_basis
delegation_report = evaluate_basis
resolve_delegation_chain = resolve_chain
chain_for = resolve_chain
scope_covers = basis_covers

# 办理授权
authorize_transaction = authorize
check_transaction = authorize
verify_authority = authorize_transaction

# 现任可办理负责人
handlers_for = eligible_handlers
current_handlers = eligible_handlers
responsible_handlers = eligible_handlers
find_handlers = eligible_handlers
list_handlers = eligible_handlers

# 事项 / 已领未办
report_case = assess_case
describe_case = assess_case
case_continuation = assess_case
list_pending = pending_matters
open_cases = pending_matters
pending_cases = pending_matters
continuation_report = build_report

# 人员身份
verify_identity = HolderDirectory
verify_agent = HolderDirectory
AgentDirectory = HolderDirectory

# 服务类别名
Agency = DelegationService
TransferService = DelegationService


def reassign_case(records_dir, case_id, *, handler, issuer=None, at,
                  basis_id=None, actions=None, until=None, accept_at=None,
                  out_dir=None, **issue_kwargs):
    """一站式重新指派：必要时由 issuer 签发新依据、handler 确认，再指派。

    若给定现成的 ``basis_id`` 且该依据已确认有效，则直接指派；否则用
    ``issuer`` 签发一条新授权，``accept_at``（默认等于 at）由 handler 确认。
    返回 service / grant / acceptance / assignment，便于取回执。
    """
    service = DelegationService(records_dir, out_dir)
    grant = acceptance = None
    if basis_id is None:
        if issuer is None or until is None:
            raise DelegationError("新指派需要 basis_id，或同时给出 issuer 与 until")
        case = service.get_case(case_id)
        grant = service.issue_grant(
            issuer=issuer, holder=handler,
            zone=issue_kwargs.pop("zone", case["zone"]),
            actions=actions or [case["action"]], until=until, at=at,
            for_case=case_id, **issue_kwargs)
        acceptance = service.accept_grant(
            grant["id"], holder=handler, at=accept_at if accept_at is not None else at)
        basis_id = grant["id"]
    result = service.assign_case(
        case_id, handler=handler, basis_id=basis_id,
        at=accept_at if accept_at is not None else at)
    return {"service": service, "grant": grant,
            "acceptance": acceptance, **result}


if __name__ == "__main__":
    raise SystemExit(main())
