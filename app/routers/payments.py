from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from .. import payments_service as svc
from ..db import get_conn
from ..errors import APIError
from ..schemas import (
    HolderLedger,
    PaymentIn,
    PaymentOut,
    PaymentRefundsOut,
    RefundIn,
    RefundOut,
)

router = APIRouter(tags=["payments"])


@router.post("/payments")
def register_payment(body: PaymentIn, conn=Depends(get_conn)):
    """登记一笔已完成付款。

    - 余额口径：全部期间已出具单据明细（初次单与正负追补单）累计额
      减去已登记付款、再加已登记退回；试算不出具、待处理播放不入账；
    - 仅当付款金额不超过当时的**正余额**时入账，返回付款记录及登记后余额；
    - 同一外部流水号同内容重试幂等返回原记录（status=duplicate，HTTP 200）；
      同流水号异内容（权利人或金额不同）返回 409 payment_ref_conflict；
    - 权利人不存在 404；金额非正整数 422；余额不为正或超额 409
      payment_exceeds_balance；拒绝时不留任何付款；
    - 跨期负额追补后历史付款不撤销，负余额留待后续正额抵扣，余额非正禁止付款；
    - 与出单、退回或其他付款并发时，行锁保证"校验 + 入账"一致，绝不超付。
    """
    result = svc.register_payment(conn, body.holder_code, body.external_ref, body.amount)
    # 新登记 201；同流水号同内容重试 200 且返回原记录
    status = 201 if result["status"] == "created" else 200
    return JSONResponse(status_code=status, content=PaymentOut(**result).model_dump(mode="json"))


@router.post("/payments/refunds")
def register_refund(body: RefundIn, conn=Depends(get_conn)):
    """登记一笔银行退票（付款退回）。

    - 凭原付款流水号 + 退回流水号 + 正整数分额登记，返回退回记录及登记后余额；
    - 原付款不存在 404 payment_not_found；金额非正整数 422；
    - 该原付款累计退回超过原付款金额 409 refund_exceeds_payment；
      上述拒绝均不留任何退回记录；
    - 同一退回流水号同内容重试幂等返回原记录（status=duplicate，HTTP 200）；
      同号异内容（原付款流水号或金额不同）返回 409 refund_ref_conflict；
    - 原付款与单据保持不可变；退回加回余额（余额可为负）；
    - 与付款、出单并发时行锁保证校验与入账一致，多个退回合计绝不超额。
    """
    result = svc.register_refund(conn, body.external_ref, body.refund_ref, body.amount)
    # 新登记 201；同退回流水号同内容重试 200 且返回原记录
    status = 201 if result["status"] == "created" else 200
    return JSONResponse(status_code=status, content=RefundOut(**result).model_dump(mode="json"))


@router.get("/payments/{external_ref}/refunds", response_model=PaymentRefundsOut)
def get_payment_refunds(external_ref: str, conn=Depends(get_conn)):
    """按原付款流水号查询：付款快照、逐笔退回、累计退回与尚可退回额。

    原付款不存在返回 404 payment_not_found。
    """
    result = svc.get_payment_refunds(conn, external_ref)
    if result is None:
        raise APIError(
            404,
            "payment_not_found",
            f"payment with external_ref {external_ref} not found",
        )
    return result


@router.get("/rightsholders/{holder_code}/ledger", response_model=HolderLedger)
def get_holder_ledger(holder_code: str, conn=Depends(get_conn)):
    """按权利人核对：单据增减、付款流水、逐笔退回与当前余额。

    - statements[] 为该权利人全部期间已出具单据的明细行（追补差额保留正负号），
      按单据出具次序排列；payments[] 为已登记付款流水；refunds[] 为逐笔退回；
    - billed_total=单据正负累计、paid_total=付款累计、refunded_total=退回累计、
      balance=billed_total-paid_total+refunded_total（可为负，即待后续正额抵扣）；
    - 待处理播放与试算不在内；权利人不存在返回 404 holder_not_found。
    """
    result = svc.get_holder_ledger(conn, holder_code)
    if result is None:
        raise APIError(
            404,
            "holder_not_found",
            f"rightsholder {holder_code} not found",
        )
    return result
