#!/usr/bin/env python3
"""退款清算台 HTTP 服务（仅标准库，完全离线）。

用法: python3 server.py [--port 8000] [--db data/refund_desk.db]
"""
import argparse
import csv
import io
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from refunddesk.store import Store, ApiError
from refunddesk.money import MoneyError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
SAMPLES_DIR = os.path.join(BASE_DIR, "samples")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".svg": "image/svg+xml",
}


def parse_csv_text(text):
    """CSV 文本 -> [{列名: 值}]；首行为表头，忽略空行与 # 注释行。"""
    reader = csv.DictReader(
        line for line in io.StringIO(text.strip())
        if line.strip() and not line.lstrip().startswith("#"))
    rows = []
    for row in reader:
        rows.append({(k or "").strip(): (v or "").strip() for k, v in row.items()})
    return rows


def to_csv(rows, headers):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return "\ufeff" + buf.getvalue()  # BOM 便于 Excel 打开


def make_handler(store):
    class Handler(BaseHTTPRequestHandler):
        server_version = "RefundDesk/1.0"

        # ---------- 基础 ----------
        def log_message(self, fmt, *args):
            pass  # 静默

        def _send(self, status, body, content_type="application/json; charset=utf-8",
                  headers=None):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _json(self, status, obj):
            self._send(status, json.dumps(obj, ensure_ascii=False))

        def _error(self, status, code, message):
            self._json(status, {"error": {"code": code, "message": message}})

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise ApiError(400, "BAD_JSON", "请求体不是合法 JSON")

        def _dispatch(self, method, path, body=None):
            # 静态
            if method == "GET" and (path == "/" or path == "/index.html"):
                return self._serve_static("index.html")
            if method == "GET" and path.startswith("/static/"):
                return self._serve_static(path[len("/static/"):])

            # API
            if method == "GET" and path == "/api/state":
                return self._json(200, store.state())
            if method == "GET" and path == "/api/verify":
                return self._json(200, store.verify())
            if method == "POST" and path == "/api/reset":
                store.reset()
                return self._json(200, {"ok": True})
            if method == "POST" and path == "/api/import/sample":
                return self._json(200, self._import_sample())
            m = re.fullmatch(r"/api/import/(orders|payments|discounts)", path)
            if method == "POST" and m:
                return self._json(200, self._import(m.group(1), body))
            if method == "POST" and path == "/api/trial":
                return self._json(200, store.trial(
                    str(body.get("order_no", "")),
                    str(body.get("amount", "")),
                    body.get("currency")))
            if method == "POST" and path == "/api/refunds":
                idem = (self.headers.get("Idempotency-Key")
                        or body.get("idempotency_key") or "")
                return self._json(200, store.freeze(
                    str(body.get("order_no", "")), str(body.get("amount", "")),
                    body.get("currency"), body.get("reason", ""), idem))
            m = re.fullmatch(r"/api/refunds/([A-Za-z0-9_-]+)/(confirm|release|reverse)", path)
            if method == "POST" and m:
                refund_no, action = m.group(1), m.group(2)
                note = (body or {}).get("note", "")
                if action == "confirm":
                    return self._json(200, store.confirm(refund_no))
                if action == "release":
                    return self._json(200, store.release(refund_no, note))
                return self._json(200, store.reverse(refund_no, note))
            if method == "GET" and path == "/api/export/reconciliation.csv":
                rows = store.reconciliation_rows()
                return self._csv("reconciliation.csv", rows, [
                    "order_no", "currency", "fx_rate", "order_total", "pool", "frozen",
                    "confirmed", "reversed", "available", "pool_base",
                    "confirmed_base", "available_base"])
            if method == "GET" and path == "/api/export/refunds.csv":
                rows = store.refund_export_rows()
                return self._csv("refunds.csv", rows, [
                    "refund_no", "order_no", "status", "line_seq", "source_type",
                    "source_ref", "method", "amount", "amount_base", "reason",
                    "created_at"])
            if method == "GET" and path == "/api/export/ledger.csv":
                rows = store.ledger_rows()
                return self._csv("ledger.csv", rows, [
                    "entry_no", "refund_no", "order_no", "action", "line_seq",
                    "source_type", "source_ref", "method", "amount", "amount_base",
                    "available_after", "note", "created_at"])
            return self._error(404, "NOT_FOUND", f"未知路由: {method} {path}")

        def _csv(self, filename, rows, headers):
            self._send(200, to_csv(rows, headers),
                       "text/csv; charset=utf-8",
                       {"Content-Disposition": f'attachment; filename="{filename}"'})

        def _serve_static(self, name):
            name = os.path.normpath(name).lstrip("/")
            if name.startswith(".."):
                return self._error(403, "FORBIDDEN", "非法路径")
            full = os.path.join(STATIC_DIR, name)
            if not os.path.isfile(full):
                return self._error(404, "NOT_FOUND", f"文件不存在: {name}")
            ext = os.path.splitext(full)[1]
            with open(full, "rb") as f:
                self._send(200, f.read(),
                           CONTENT_TYPES.get(ext, "application/octet-stream"))

        def _import(self, kind, body):
            text = (body or {}).get("csv")
            rows = parse_csv_text(text) if text else (body or {}).get("rows", [])
            if not rows:
                raise ApiError(400, "IMPORT_EMPTY", "没有可导入的数据行")
            if kind == "orders":
                return store.import_orders(rows)
            if kind == "payments":
                return store.import_payments(rows)
            return store.import_discounts(rows)

        def _import_sample(self):
            result = {}
            for kind in ("orders", "payments", "discounts"):
                with open(os.path.join(SAMPLES_DIR, f"{kind}.csv"),
                          encoding="utf-8") as f:
                    rows = parse_csv_text(f.read())
                if kind == "orders":
                    result[kind] = store.import_orders(rows)
                elif kind == "payments":
                    result[kind] = store.import_payments(rows)
                else:
                    result[kind] = store.import_discounts(rows)
            return result

        # ---------- HTTP 方法 ----------
        def do_GET(self):
            try:
                self._dispatch("GET", self.path.split("?")[0])
            except ApiError as e:
                self._error(e.status, e.code, e.message)
            except MoneyError as e:
                self._error(400, "MONEY_INVALID", str(e))
            except Exception as e:  # noqa: BLE001
                self._error(500, "INTERNAL", f"{type(e).__name__}: {e}")

        def do_POST(self):
            try:
                self._dispatch("POST", self.path.split("?")[0], self._body())
            except ApiError as e:
                self._error(e.status, e.code, e.message)
            except MoneyError as e:
                self._error(400, "MONEY_INVALID", str(e))
            except Exception as e:  # noqa: BLE001
                self._error(500, "INTERNAL", f"{type(e).__name__}: {e}")

    return Handler


def create_server(port, db_path):
    store = Store(db_path)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store))
    server.store = store
    return server


def main():
    ap = argparse.ArgumentParser(description="离线退款清算台")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default=os.path.join(BASE_DIR, "data", "refund_desk.db"))
    args = ap.parse_args()
    server = create_server(args.port, args.db)
    print(f"退款清算台已启动: http://127.0.0.1:{args.port}  (数据库: {args.db})")
    print("Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
