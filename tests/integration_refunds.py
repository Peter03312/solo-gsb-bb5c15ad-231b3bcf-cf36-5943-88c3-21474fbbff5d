"""银行退票（付款退回）与回单核验退回额冻结：端到端集成测试（pgserver 便携 PostgreSQL，无需 Docker）。

运行: PYTHONPATH=. python3 tests/integration_refunds.py
"""
import os
import pathlib
import tempfile
import threading

import pgserver

_pgdata = pathlib.Path(tempfile.mkdtemp(prefix="royalty_rfnd_")) / "pgdata"
server = pgserver.get_server(_pgdata, cleanup_mode="delete")
try:
    os.environ["DATABASE_URL"] = server.get_uri()
    # 配置必须在设置环境变量后导入
    from fastapi.testclient import TestClient
    import psycopg
    from app import db
    from app.main import app

    failures = []

    def check(name, cond, extra=""):
        print(("PASS" if cond else "FAIL"), name, extra)
        if not cond:
            failures.append(name)

    def pay(holder, ref, amount):
        r = c.post("/payments", json={
            "holder_code": holder, "external_ref": ref, "amount": amount})
        assert r.status_code == 201, r.text
        return r

    def refund(ref, rref, amount):
        return c.post("/payments/refunds", json={
            "external_ref": ref, "refund_ref": rref, "amount": amount})

    with TestClient(app) as c:
        check("health", c.get("/health").json()["status"] == "ok")

        # ---- 建档：作品 + 权利人 A/B，协议 60/40，播放 1000 => A 600 / B 400 ----
        c.post("/works", json={"code": "W", "title": "作品"})
        c.post("/rightsholders", json={"code": "A", "name": "甲"})
        c.post("/rightsholders", json={"code": "B", "name": "乙"})
        c.post("/agreements", json={
            "work_code": "W",
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "A", "percent": "60"},
                       {"holder_code": "B", "percent": "40"}]})
        c.post("/plays/import", json={
            "import_id": "P1", "work_code": "W",
            "occurred_at": "2026-02-10T20:00:00+08:00", "amount": 1000})
        c.post("/statements/issue", json={
            "start": "2026-02-01T00:00:00+08:00",
            "end": "2026-03-01T00:00:00+08:00"})

        # ---- 原付款不存在 => 404，不留记录 ----
        r = refund("NOPE", "RET-0", 1)
        check("refund of unknown payment 404", r.status_code == 404
              and r.json()["error"] == "payment_not_found", r.text)
        check("unknown payment refunds query 404",
              c.get("/payments/NOPE/refunds").status_code == 404)

        # ---- 金额/字段校验：非正整数、空编号 => 422 ----
        for i, bad in enumerate([
            {"external_ref": "PAY-A", "refund_ref": "RB-1", "amount": 0},
            {"external_ref": "PAY-A", "refund_ref": "RB-1", "amount": -5},
            {"external_ref": "PAY-A", "refund_ref": "RB-1", "amount": 1.5},
            {"external_ref": "PAY-A", "refund_ref": "RB-1", "amount": True},
            {"external_ref": "PAY-A", "refund_ref": "RB-1", "amount": "100"},
            {"external_ref": "", "refund_ref": "RB-1", "amount": 1},
            {"external_ref": "PAY-A", "refund_ref": "", "amount": 1},
            {"external_ref": "PAY-A", "refund_ref": "RB-1"},
        ]):
            r = c.post("/payments/refunds", json=bad)
            check(f"invalid refund field 422 #{i}", r.status_code == 422, r.text)

        # ---- A 全额付款 600（余额归零）后登记退回 ----
        pay("A", "PAY-A", 600)
        check("A balance zero after full pay",
              c.get("/rightsholders/A/ledger").json()["balance"] == 0)

        r = refund("PAY-A", "RET-1", 200)
        body = r.json()
        check("refund created 201", r.status_code == 201
              and body["status"] == "created"
              and body["external_ref"] == "PAY-A"
              and body["refund_ref"] == "RET-1"
              and body["amount"] == 200
              and body["holder_code"] == "A"
              and body["balance_after"] == 200, r.text)  # 600-600+200

        # 同退回流水号同内容重试 => 幂等返回原记录，不重复入账
        r = refund("PAY-A", "RET-1", 200)
        body2 = r.json()
        check("refund idempotent retry", r.status_code == 200
              and body2["status"] == "duplicate"
              and body2["refund_id"] == body["refund_id"]
              and body2["balance_after"] == 200, r.text)
        check("retry no double refund",
              c.get("/payments/PAY-A/refunds").json()["refunded_total"] == 200)

        # 同退回流水号异内容（金额不同 / 原付款不同）=> 409，不改账
        r = refund("PAY-A", "RET-1", 201)
        check("refund_ref conflict amount", r.status_code == 409
              and r.json()["error"] == "refund_ref_conflict"
              and r.json()["detail"]["existing_amount"] == 200, r.text)
        pay("B", "PAY-B", 400)
        r = refund("PAY-B", "RET-1", 200)
        check("refund_ref conflict payment", r.status_code == 409
              and r.json()["error"] == "refund_ref_conflict"
              and r.json()["detail"]["existing_external_ref"] == "PAY-A", r.text)
        check("conflict leaves no extra refund",
              c.get("/payments/PAY-B/refunds").json()["refunded_total"] == 0)

        # ---- 多次部分退回，累计不得超原付款 ----
        r = refund("PAY-A", "RET-2", 300)
        check("second partial refund", r.status_code == 201
              and r.json()["balance_after"] == 500, r.text)  # 600-600+500
        r = refund("PAY-A", "RET-3", 101)  # 累计 701 > 600
        check("cumulative refund exceeds 409", r.status_code == 409
              and r.json()["error"] == "refund_exceeds_payment"
              and r.json()["detail"]["payment_amount"] == 600
              and r.json()["detail"]["refunded_total"] == 500
              and r.json()["detail"]["requested_amount"] == 101, r.text)
        # 拒绝不留记录
        q = c.get("/payments/PAY-A/refunds").json()
        check("exceeds leaves no record",
              q["refunded_total"] == 500 and len(q["refunds"]) == 2
              and q["remaining_refundable"] == 100, str(q))
        # 恰好退满原付款额
        r = refund("PAY-A", "RET-3", 100)
        check("refund up to exact payment amount", r.status_code == 201
              and r.json()["balance_after"] == 600, r.text)
        q = c.get("/payments/PAY-A/refunds").json()
        check("fully refunded", q["refunded_total"] == 600
              and q["remaining_refundable"] == 0
              and [x["refund_ref"] for x in q["refunds"]]
              == ["RET-1", "RET-2", "RET-3"], str(q))
        r = refund("PAY-A", "RET-4", 1)
        check("beyond full payment 409", r.status_code == 409
              and r.json()["error"] == "refund_exceeds_payment", r.text)

        # ---- 对账：保留付款总额，另列退回总额、逐笔退回及余额 ----
        led = c.get("/rightsholders/A/ledger").json()
        check("ledger A with refunds",
              led["billed_total"] == 600 and led["paid_total"] == 600
              and led["refunded_total"] == 600 and led["balance"] == 600
              and [x["refund_ref"] for x in led["refunds"]]
              == ["RET-1", "RET-2", "RET-3"]
              and all(x["external_ref"] == "PAY-A" for x in led["refunds"]),
              str({k: led[k] for k in ("billed_total", "paid_total",
                                       "refunded_total", "balance")}))
        # 退回后余额回升，仍只允许在正余额内付款
        r = c.post("/payments", json={
            "holder_code": "A", "external_ref": "PAY-A2", "amount": 600})
        check("pay again within recovered balance", r.status_code == 201
              and r.json()["balance_after"] == 0, r.text)
        r = c.post("/payments", json={
            "holder_code": "A", "external_ref": "PAY-A3", "amount": 1})
        check("still no overpay after refunds", r.status_code == 409
              and r.json()["error"] == "payment_exceeds_balance", r.text)

        # ---- B 部分退回：余额 = 单据 - 付款 + 退回 ----
        r = refund("PAY-B", "RET-B1", 100)
        check("B partial refund", r.status_code == 201
              and r.json()["balance_after"] == 100, r.text)  # 400-400+100
        led = c.get("/rightsholders/B/ledger").json()
        check("ledger B with refund",
              led["paid_total"] == 400 and led["refunded_total"] == 100
              and led["balance"] == 100
              and led["refunds"][0]["refund_ref"] == "RET-B1", str(led))
        r = c.post("/payments", json={
            "holder_code": "B", "external_ref": "PAY-B2", "amount": 100})
        check("B pay recovered amount", r.status_code == 201
              and r.json()["balance_after"] == 0, r.text)

        # ---- 退回记录不可变（触发器强制），原付款同样不可变 ----
        with db.pool.connection() as db_conn:
            frozen = []
            for sql in (
                "UPDATE payment_refunds SET amount = 1 WHERE refund_ref = 'RET-1'",
                "DELETE FROM payment_refunds WHERE refund_ref = 'RET-1'",
            ):
                try:
                    db_conn.execute(sql)
                    frozen.append(False)
                except psycopg.Error:
                    frozen.append(True)
        check("refunds frozen update/delete", all(frozen), str(frozen))
        check("refund data intact",
              c.get("/payments/PAY-A/refunds").json()["refunded_total"] == 600)

        # ---- 并发：同一退回流水号同内容并发，恰一笔登记，其余幂等 ----
        c.post("/rightsholders", json={"code": "C", "name": "丙"})
        c.post("/works", json={"code": "W2", "title": "作品二"})
        c.post("/agreements", json={
            "work_code": "W2",
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "C", "percent": "100"}]})
        c.post("/plays/import", json={
            "import_id": "PC-1", "work_code": "W2",
            "occurred_at": "2026-02-15T20:00:00+08:00", "amount": 999})
        c.post("/statements/issue", json={
            "start": "2026-02-01T00:00:00+08:00",
            "end": "2026-03-01T00:00:00+08:00"})
        pay("C", "PAY-C", 999)

        same_results = []

        def fire_same_refund():
            rr = refund("PAY-C", "RET-C", 100)
            same_results.append((rr.status_code, rr.json()))

        ts = [threading.Thread(target=fire_same_refund) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        ids = {b["refund_id"] for _, b in same_results}
        created = sum(1 for _, b in same_results if b["status"] == "created")
        dup = sum(1 for _, b in same_results if b["status"] == "duplicate")
        check("same refund_ref concurrent serialized",
              len(ids) == 1 and created == 1 and dup == 7,
              f"{[(s, b['status']) for s, b in same_results]}")
        check("C refunded 100 once",
              c.get("/payments/PAY-C/refunds").json()["refunded_total"] == 100)

        # ---- 并发：多个退回（不同退回流水号）对同一原付款，合计不得超额 ----
        # PAY-C 999 已退 100，尚可退 899；20 路各退 100 => 恰 8 笔成功（800），其余拒绝
        outcomes = {"created": 0, "rejected": 0, "errors": []}

        def fire_distinct_refund(i):
            try:
                rr = refund("PAY-C", f"RET-C-{i}", 100)
                if rr.status_code == 201:
                    outcomes["created"] += 1
                elif (rr.status_code == 409
                      and rr.json()["error"] == "refund_exceeds_payment"):
                    outcomes["rejected"] += 1
                else:
                    outcomes["errors"].append((rr.status_code, rr.text))
            except Exception as e:  # noqa
                outcomes["errors"].append(repr(e))

        ts = [threading.Thread(target=fire_distinct_refund, args=(i,))
              for i in range(20)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        q = c.get("/payments/PAY-C/refunds").json()
        check("concurrent refunds never exceed payment",
              not outcomes["errors"]
              and outcomes["created"] == 8 and outcomes["rejected"] == 12
              and q["refunded_total"] == 900
              and q["remaining_refundable"] == 99,
              f"{outcomes} {q['refunded_total']}")

        # ---- 并发：退回、付款与出单多轮同时触发，余额校验一致 ----
        # 每轮：新播放 100（A 得 60）出单 => 付 PAY-R{i} 60（必成功）；
        # 随后 Barrier 同时放行"退 PAY-R{i} 60"与"再付 PAY-X{i} 60"：
        # 退先则余额回升 60、付成功；付先则余额为 0、付被拒。两种次序均合法，
        # 不变量：净付款（付款-退回）不超过单据累计，且每个原付款退回不超额。
        rounds = 15
        stats = {"pay_created": 0, "pay_rejected": 0, "refund_created": 0,
                 "errors": []}

        def one_round(i):
            c.post("/plays/import", json={
                "import_id": f"PR-{i}", "work_code": "W",
                "occurred_at": f"2026-02-{(i % 20) + 1:02d}T20:00:00+08:00",
                "amount": 100})
            c.post("/statements/issue", json={
                "start": "2026-02-01T00:00:00+08:00",
                "end": "2026-03-01T00:00:00+08:00"})
            pay("A", f"PAY-R{i}", 60)
            barrier = threading.Barrier(2)

            def refund_once():
                barrier.wait()
                rr = refund(f"PAY-R{i}", f"RET-R{i}", 60)
                if rr.status_code == 201:
                    stats["refund_created"] += 1
                else:
                    stats["errors"].append(("refund", rr.status_code, rr.text))

            def pay_once():
                barrier.wait()
                rr = c.post("/payments", json={
                    "holder_code": "A", "external_ref": f"PAY-X{i}", "amount": 60})
                if rr.status_code == 201:
                    stats["pay_created"] += 1
                elif (rr.status_code == 409
                      and rr.json()["error"] == "payment_exceeds_balance"):
                    stats["pay_rejected"] += 1
                else:
                    stats["errors"].append(("pay", rr.status_code, rr.text))

            tr = threading.Thread(target=refund_once)
            tp = threading.Thread(target=pay_once)
            tr.start(); tp.start(); tr.join(); tp.join()

        for i in range(rounds):
            one_round(i)

        led = c.get("/rightsholders/A/ledger").json()
        check("refund/pay/issue multi-round consistent",
              not stats["errors"]
              and stats["refund_created"] == rounds
              and stats["pay_created"] + stats["pay_rejected"] == rounds
              and stats["pay_created"] >= 1  # 多轮中必然观察到"退先付后"
              and led["paid_total"] - led["refunded_total"] <= led["billed_total"]
              and led["balance"]
              == led["billed_total"] - led["paid_total"] + led["refunded_total"]
              and led["balance"] >= 0,
              f"{stats} {led['billed_total']}/{led['paid_total']}"
              f"/{led['refunded_total']}/{led['balance']}")

        # ---- 回单核验：另冻结对应付款累计退回额；匹配判定仍按原付款字段 ----
        c.post("/rightsholders", json={"code": "D", "name": "丁"})
        c.post("/works", json={"code": "W3", "title": "作品三"})
        c.post("/agreements", json={
            "work_code": "W3",
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "D", "percent": "100"}]})
        c.post("/plays/import", json={
            "import_id": "PD-1", "work_code": "W3",
            "occurred_at": "2026-04-10T20:00:00+08:00", "amount": 1000})
        c.post("/statements/issue", json={
            "start": "2026-04-01T00:00:00+08:00",
            "end": "2026-05-01T00:00:00+08:00"})
        pay("D", "PAY-V1", 100)
        pay("D", "PAY-V2", 50)

        def line(receipt_no, ref, holder, amount):
            return {"receipt_no": receipt_no, "external_ref": ref,
                    "holder_code": holder, "amount": amount}

        def diff_by_no(verification):
            return {d["receipt_no"]: d for d in verification["differences"]}

        r = c.post("/receipts/import", json={"batch_no": "RB1", "receipts": [
            line("R-V1", "PAY-V1", "D", 100),   # 匹配（退回后仍匹配）
            line("R-V2", "PAY-V2", "D", 51),    # 金额不符（差异行可见退回额）
            line("R-V3", "PAY-V3", "D", 10),    # 付款未登记
        ]})
        body = r.json()
        check("RB1 import 201", r.status_code == 201, r.text)
        v1 = body["verification"]
        check("RB1 v1 counts", v1["counts"] == {
            "matched": 1, "payment_not_registered": 1,
            "mismatched": 1, "duplicate": 0, "total": 3}, str(v1["counts"]))
        d = diff_by_no(v1)
        check("v1 frozen refunded totals",
              d["R-V2"]["payment_refunded_total"] == 0
              and d["R-V3"]["payment_refunded_total"] is None, str(d))

        # 登记退回：PAY-V1 退 30、PAY-V2 退 20
        refund("PAY-V1", "RET-V1", 30)
        refund("PAY-V2", "RET-V2", 20)

        # 复核：核验结论未变（仍按原付款字段判定），但退回额变化 => 新版本
        r = c.post("/receipts/RB1/reverify")
        body = r.json()
        check("reverify after refund creates v2", r.status_code == 201
              and body["status"] == "created"
              and body["verification"]["version"] == 2, r.text)
        v2 = body["verification"]
        check("v2 counts unchanged (refund does not affect matching)",
              v2["counts"] == v1["counts"], str(v2["counts"]))
        d2 = diff_by_no(v2)
        check("v2 difference row carries new refunded total",
              d2["R-V2"]["payment_refunded_total"] == 20
              and d2["R-V2"]["status"] == "mismatched"
              and d2["R-V2"]["differences"] == ["amount_mismatch"], str(d2))

        # 旧版结论不改写：v1 仍冻结退回额 0
        old = c.get("/receipts/RB1/versions/1").json()
        check("v1 not rewritten",
              diff_by_no(old)["R-V2"]["payment_refunded_total"] == 0, str(old))

        # 结论与退回额均无变化 => 不新增版本
        r = c.post("/receipts/RB1/reverify")
        check("reverify unchanged after refunds frozen",
              r.status_code == 200 and r.json()["status"] == "unchanged"
              and r.json()["verification"]["version"] == 2, r.text)

        # 再退一笔 => 退回额再次变化 => 第 3 版
        refund("PAY-V1", "RET-V1B", 10)
        r = c.post("/receipts/RB1/reverify")
        check("second refund creates v3", r.status_code == 201
              and r.json()["verification"]["version"] == 3, r.text)
        versions = c.get("/receipts/RB1/versions").json()["versions"]
        check("RB1 has 3 versions",
              [v["version"] for v in versions] == [1, 2, 3], str(versions))

        # 重复回单仍不参与核验：退回额为空
        r = c.post("/receipts/import", json={"batch_no": "RB2", "receipts": [
            line("R-V9", "PAY-V1", "D", 100)]})
        body = r.json()
        check("RB2 duplicate receipt", r.status_code == 201
              and body["receipts"][0]["is_duplicate"] is True, r.text)
        d = diff_by_no(body["verification"])
        check("duplicate receipt not verified (no refunded total)",
              d["R-V9"]["status"] == "duplicate"
              and d["R-V9"]["payment_refunded_total"] is None, str(d))

        # ---- 核验与退票均不改写付款、余额或单据 ----
        led = c.get("/rightsholders/D/ledger").json()
        check("D ledger reflects refunds, verification rewrote nothing",
              led["billed_total"] == 1000
              and led["paid_total"] == 150
              and led["refunded_total"] == 60
              and led["balance"] == 1000 - 150 + 60
              and len(led["payments"]) == 2
              and len(led["refunds"]) == 3, str(led))

    print()
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        raise SystemExit(1)
    print("ALL REFUND INTEGRATION CHECKS PASSED")
finally:
    server._cleanup()
    import shutil
    shutil.rmtree(_pgdata.parent, ignore_errors=True)
