"""付款登记、银行退票（付款退回）与权利人对账。

余额口径：全部期间已出具单据明细行（初次单与正负追补单）金额的代数和，
减去全部已登记付款，再加上全部已登记退回（银行退票加回余额）。
试算（preview）不出具、待处理播放不计入应付，因此二者天然不进入余额。

并发协议（与出单互斥，杜绝超付/超额退回）：
- 登记付款先取"外部流水号事务咨询锁"（序列化同一流水号的并发重试），
  再对该权利人行取行锁；余额校验与付款插入在同一事务内原子完成。
- 登记退回先取"退回流水号事务咨询锁"，再对原付款所属权利人行取行锁；
  同一原付款的并发退回在此串行，累计退回校验与入账一致，
  多个退回合计绝不超额。
- 出单（statements_service）在写任何明细行前，按权利人编号排序锁定
  本次受影响的全部权利人行。各方都持有行锁才写账，
  故"退回、付款与出单"并发时校验所见即所入账，不会超付、不会超额退回；
  出单侧统一按编号升序取锁，无死锁。
"""
import hashlib

# 付款流水号咨询锁命名空间
_PAYMENT_REF_NS = 0x50415931  # "PAY1"
# 退回流水号咨询锁命名空间
_REFUND_REF_NS = 0x52464431  # "RFD1"


def _combined_key(namespace: int, key: int) -> int:
    n = ((namespace & 0xFFFFFFFF) << 32) | (key & 0xFFFFFFFF)
    return n - (1 << 64) if n >= (1 << 63) else n


def _ref_lock_key(external_ref: str) -> int:
    digest = hashlib.blake2b(external_ref.encode(), digest_size=8).digest()
    n = int.from_bytes(digest, "big", signed=False)
    return n - (1 << 64) if n >= (1 << 63) else n


# 全部期间已出具单据的正负金额累计；追补差额为有符号值，直接代数相加。
# 待处理播放不入 statement_lines，故不影响余额；preview 根本不出单。
_BILLED_TOTAL_SQL = """
SELECT COALESCE(SUM(amount), 0) AS billed
FROM statement_lines
WHERE holder_code = %s
"""

_PAID_TOTAL_SQL = """
SELECT COALESCE(SUM(amount), 0) AS paid
FROM payments
WHERE holder_code = %s
"""

# 已登记退回（银行退票）累计：加回余额
_REFUNDED_TOTAL_SQL = """
SELECT COALESCE(SUM(amount), 0) AS refunded
FROM payment_refunds
WHERE holder_code = %s
"""


def _billed_total(conn, holder_code: str) -> int:
    return int(conn.execute(_BILLED_TOTAL_SQL, (holder_code,)).fetchone()["billed"])


def _paid_total(conn, holder_code: str) -> int:
    return int(conn.execute(_PAID_TOTAL_SQL, (holder_code,)).fetchone()["paid"])


def _refunded_total(conn, holder_code: str) -> int:
    return int(conn.execute(_REFUNDED_TOTAL_SQL, (holder_code,)).fetchone()["refunded"])


def get_balance(conn, holder_code: str) -> int:
    """当前余额 = 已出具单据累计 - 已登记付款 + 已登记退回（可为负）。"""
    return (
        _billed_total(conn, holder_code)
        - _paid_total(conn, holder_code)
        + _refunded_total(conn, holder_code)
    )


def lock_holders_for_billing(conn, holder_codes) -> None:
    """出单侧调用：按编号升序逐个锁定本次受影响的权利人，再写单据明细。

    与付款登记持有的权利人行锁互斥；逐个、升序加锁，使多权利人事务间
    加锁次序全局一致，避免死锁（不依赖数据库执行计划的取行顺序）。
    """
    codes = sorted({c for c in holder_codes if c})
    for code in codes:
        conn.execute(
            "SELECT code FROM rightsholders WHERE code = %s FOR UPDATE",
            (code,),
        )


def register_payment(conn, holder_code: str, external_ref: str, amount: int) -> dict:
    """登记一笔已完成付款。

    - 权利人不存在：404 holder_not_found，不留付款；
    - 同一流水号同内容重试：返回原付款记录与登记后余额（幂等）；
    - 同一流水号异内容（权利人或金额不同）：409 payment_ref_conflict；
    - 当前正余额不足（余额 <= 0 或 amount > balance）：
      409 payment_exceeds_balance，拒绝且不留付款。
    """
    with conn.transaction():
        # 先按流水号加锁：并发的同流水号请求在此串行化，
        # 避免唯一约束竞态下误报，也保证"同内容/异内容"判定确定。
        conn.execute(
            "SELECT pg_advisory_xact_lock(%s::bigint)",
            (_combined_key(_PAYMENT_REF_NS, _ref_lock_key(external_ref)),),
        )

        existing = conn.execute(
            "SELECT id, holder_code, amount, created_at FROM payments WHERE external_ref = %s",
            (external_ref,),
        ).fetchone()
        if existing is not None:
            if (
                existing["holder_code"] != holder_code
                or int(existing["amount"]) != amount
            ):
                raise _conflict(external_ref, existing)
            return {
                "payment_id": existing["id"],
                "holder_code": existing["holder_code"],
                "external_ref": external_ref,
                "amount": int(existing["amount"]),
                "registered_at": existing["created_at"],
                "status": "duplicate",
                "balance_after": get_balance(conn, existing["holder_code"]),
            }

        # 锁定权利人行：与出单侧行锁互斥；权利人不存在则 404。
        holder = conn.execute(
            "SELECT code FROM rightsholders WHERE code = %s FOR UPDATE",
            (holder_code,),
        ).fetchone()
        if holder is None:
            raise _holder_not_found(holder_code)

        balance = get_balance(conn, holder_code)
        if balance <= 0 or amount > balance:
            raise _exceeds_balance(holder_code, amount, balance)

        row = conn.execute(
            """
            INSERT INTO payments(holder_code, external_ref, amount)
            VALUES (%s, %s, %s)
            RETURNING id, created_at
            """,
            (holder_code, external_ref, amount),
        ).fetchone()
        return {
            "payment_id": row["id"],
            "holder_code": holder_code,
            "external_ref": external_ref,
            "amount": amount,
            "registered_at": row["created_at"],
            "status": "created",
            "balance_after": balance - amount,
        }


def _holder_not_found(holder_code: str):
    from .errors import APIError

    return APIError(
        404,
        "holder_not_found",
        f"rightsholder {holder_code} not found",
    )


def _conflict(external_ref: str, existing) -> "object":
    from .errors import APIError

    return APIError(
        409,
        "payment_ref_conflict",
        f"external payment ref {external_ref} already registered with different content",
        detail={
            "external_ref": external_ref,
            "existing_holder_code": existing["holder_code"],
            "existing_amount": int(existing["amount"]),
        },
    )


def _exceeds_balance(holder_code: str, amount: int, balance: int):
    from .errors import APIError

    return APIError(
        409,
        "payment_exceeds_balance",
        f"payment amount {amount} exceeds current positive balance {balance} "
        f"of rightsholder {holder_code}; payment rejected",
        detail={
            "holder_code": holder_code,
            "requested_amount": amount,
            "balance": balance,
        },
    )


# ---- 银行退票（付款退回）----

def register_refund(conn, external_ref: str, refund_ref: str, amount: int) -> dict:
    """登记一笔银行退票（付款退回）。

    - 原付款不存在：404 payment_not_found，不留记录；
    - 同一退回流水号同内容重试：返回原退回记录与登记后余额（幂等）；
    - 同一退回流水号异内容（原付款流水号或金额不同）：409 refund_ref_conflict；
    - 该原付款累计退回 + 本次金额超过原付款金额：
      409 refund_exceeds_payment，拒绝且不留记录；
    - 原付款与单据保持不可变，退回只新增退回记录、加回余额。
    """
    with conn.transaction():
        # 先按退回流水号加锁：并发的同号请求在此串行化，
        # 避免唯一约束竞态下误报，也保证"同内容/异内容"判定确定。
        conn.execute(
            "SELECT pg_advisory_xact_lock(%s::bigint)",
            (_combined_key(_REFUND_REF_NS, _ref_lock_key(refund_ref)),),
        )

        existing = conn.execute(
            """
            SELECT f.id, f.payment_id, f.holder_code, f.amount, f.created_at,
                   p.external_ref
            FROM payment_refunds f
            JOIN payments p ON p.id = f.payment_id
            WHERE f.refund_ref = %s
            """,
            (refund_ref,),
        ).fetchone()
        if existing is not None:
            if (
                existing["external_ref"] != external_ref
                or int(existing["amount"]) != amount
            ):
                raise _refund_conflict(refund_ref, existing)
            return {
                "refund_id": existing["id"],
                "payment_id": existing["payment_id"],
                "holder_code": existing["holder_code"],
                "external_ref": existing["external_ref"],
                "refund_ref": refund_ref,
                "amount": int(existing["amount"]),
                "registered_at": existing["created_at"],
                "status": "duplicate",
                "balance_after": get_balance(conn, existing["holder_code"]),
            }

        payment = conn.execute(
            "SELECT id, holder_code, amount FROM payments WHERE external_ref = %s",
            (external_ref,),
        ).fetchone()
        if payment is None:
            raise _payment_not_found(external_ref)

        # 锁定权利人行：与付款登记、出单侧行锁互斥；同一原付款的并发退回
        # 在此串行，累计退回校验与入账一致，多个退回合计绝不超额。
        conn.execute(
            "SELECT code FROM rightsholders WHERE code = %s FOR UPDATE",
            (payment["holder_code"],),
        )

        refunded = int(
            conn.execute(
                "SELECT COALESCE(SUM(amount), 0) AS refunded "
                "FROM payment_refunds WHERE payment_id = %s",
                (payment["id"],),
            ).fetchone()["refunded"]
        )
        if refunded + amount > int(payment["amount"]):
            raise _refund_exceeds(
                external_ref, int(payment["amount"]), refunded, amount
            )

        row = conn.execute(
            """
            INSERT INTO payment_refunds(payment_id, holder_code, refund_ref, amount)
            VALUES (%s, %s, %s, %s)
            RETURNING id, created_at
            """,
            (payment["id"], payment["holder_code"], refund_ref, amount),
        ).fetchone()
        return {
            "refund_id": row["id"],
            "payment_id": payment["id"],
            "holder_code": payment["holder_code"],
            "external_ref": external_ref,
            "refund_ref": refund_ref,
            "amount": amount,
            "registered_at": row["created_at"],
            "status": "created",
            "balance_after": get_balance(conn, payment["holder_code"]),
        }


def get_payment_refunds(conn, external_ref: str):
    """按原付款流水号查询：付款快照、逐笔退回、累计退回与尚可退回额。"""
    payment = conn.execute(
        "SELECT id, holder_code, amount FROM payments WHERE external_ref = %s",
        (external_ref,),
    ).fetchone()
    if payment is None:
        return None
    rows = conn.execute(
        """
        SELECT id, refund_ref, amount, created_at
        FROM payment_refunds
        WHERE payment_id = %s
        ORDER BY id
        """,
        (payment["id"],),
    ).fetchall()
    refunded_total = sum(int(r["amount"]) for r in rows)
    return {
        "payment_id": payment["id"],
        "external_ref": external_ref,
        "holder_code": payment["holder_code"],
        "payment_amount": int(payment["amount"]),
        "refunded_total": refunded_total,
        "remaining_refundable": int(payment["amount"]) - refunded_total,
        "refunds": [
            {
                "refund_id": r["id"],
                "payment_id": payment["id"],
                "external_ref": external_ref,
                "refund_ref": r["refund_ref"],
                "amount": int(r["amount"]),
                "registered_at": r["created_at"],
            }
            for r in rows
        ],
    }


def _payment_not_found(external_ref: str):
    from .errors import APIError

    return APIError(
        404,
        "payment_not_found",
        f"payment with external_ref {external_ref} not found",
    )


def _refund_conflict(refund_ref: str, existing):
    from .errors import APIError

    return APIError(
        409,
        "refund_ref_conflict",
        f"refund ref {refund_ref} already registered with different content",
        detail={
            "refund_ref": refund_ref,
            "existing_external_ref": existing["external_ref"],
            "existing_amount": int(existing["amount"]),
        },
    )


def _refund_exceeds(external_ref: str, payment_amount: int,
                    refunded_total: int, amount: int):
    from .errors import APIError

    return APIError(
        409,
        "refund_exceeds_payment",
        f"refund amount {amount} would bring total refunds of payment "
        f"{external_ref} to {refunded_total + amount}, exceeding the original "
        f"payment amount {payment_amount}; refund rejected",
        detail={
            "external_ref": external_ref,
            "payment_amount": payment_amount,
            "refunded_total": refunded_total,
            "requested_amount": amount,
        },
    )


def get_holder_ledger(conn, holder_code: str):
    """按权利人查询：基本信息、单据增减、付款流水、逐笔退回与当前余额。"""
    holder = conn.execute(
        "SELECT code, name FROM rightsholders WHERE code = %s",
        (holder_code,),
    ).fetchone()
    if holder is None:
        return None

    statement_rows = conn.execute(
        """
        SELECT b.id AS batch_id, b.kind, b.period_start, b.period_end,
               b.created_at AS issued_at, l.amount
        FROM statement_lines l
        JOIN statement_batches b ON b.id = l.batch_id
        WHERE l.holder_code = %s
        ORDER BY b.id
        """,
        (holder_code,),
    ).fetchall()

    payment_rows = conn.execute(
        """
        SELECT id, external_ref, amount, created_at
        FROM payments
        WHERE holder_code = %s
        ORDER BY id
        """,
        (holder_code,),
    ).fetchall()

    refund_rows = conn.execute(
        """
        SELECT f.id, f.payment_id, p.external_ref, f.refund_ref,
               f.amount, f.created_at
        FROM payment_refunds f
        JOIN payments p ON p.id = f.payment_id
        WHERE f.holder_code = %s
        ORDER BY f.id
        """,
        (holder_code,),
    ).fetchall()

    billed_total = sum(int(r["amount"]) for r in statement_rows)
    paid_total = sum(int(r["amount"]) for r in payment_rows)
    refunded_total = sum(int(r["amount"]) for r in refund_rows)

    return {
        "holder_code": holder["code"],
        "name": holder["name"],
        "billed_total": billed_total,
        "paid_total": paid_total,
        "refunded_total": refunded_total,
        "balance": billed_total - paid_total + refunded_total,
        "statements": [
            {
                "batch_id": r["batch_id"],
                "kind": r["kind"],
                "period_start": r["period_start"],
                "period_end": r["period_end"],
                "amount": int(r["amount"]),
                "issued_at": r["issued_at"],
            }
            for r in statement_rows
        ],
        "payments": [
            {
                "payment_id": r["id"],
                "external_ref": r["external_ref"],
                "amount": int(r["amount"]),
                "registered_at": r["created_at"],
            }
            for r in payment_rows
        ],
        "refunds": [
            {
                "refund_id": r["id"],
                "payment_id": r["payment_id"],
                "external_ref": r["external_ref"],
                "refund_ref": r["refund_ref"],
                "amount": int(r["amount"]),
                "registered_at": r["created_at"],
            }
            for r in refund_rows
        ],
    }
