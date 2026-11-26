"""Durable tenant usage, per-request price snapshots, and optional content history."""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import logging
import math
import os
import re
import sqlite3
import time

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field


log = logging.getLogger("muse2api")
VIETNAM = timezone(timedelta(hours=7))
CONTENT_LIMIT = 16000
SECRET = re.compile(
    r"(?i)\b(?:https?|socks5h?)://[^\s]+|\bm2[ac]_[\w.-]+|\bsk-[\w-]{10,}|"
    r"\bAIza[\w-]{30,}|\bBearer\s+[\w./+=-]+|\bhatch_\w+=[^\s;]+"
)


class Price(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: int | None = Field(default=None, ge=0, le=100_000_000)
    image: int | None = Field(default=None, ge=0, le=100_000_000)
    video_second: int | None = Field(default=None, ge=0, le=100_000_000)
    input_1k_tokens: int | None = Field(default=None, ge=0, le=100_000_000)
    output_1k_tokens: int | None = Field(default=None, ge=0, le=100_000_000)


def redact_content(text):
    return SECRET.sub("[đã ẩn]", str(text))[:CONTENT_LIMIT] if text else None


def usage_from_response(data):
    reported = data.get("usage")
    if not isinstance(reported, dict):
        return {}
    usage = {}
    for key, alias in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens")):
        value = reported.get(key, reported.get(alias))
        if type(value) is int and 0 <= value <= 1_000_000_000:
            usage[key] = value
    return usage


def _total_price(prices, usage):
    if not prices:
        return None
    units = {"request": 1, "image": usage.get("images"), "video_second": usage.get("video_seconds")}
    for key in ("input_tokens", "output_tokens"):
        count = usage.get(key)
        units[key.replace("_tokens", "_1k_tokens")] = Decimal(count) / 1000 if count is not None else None
    total = Decimal(0)
    for unit, price in prices.items():
        count = units[unit]
        if price and count is None:
            return None
        if price:
            total += Decimal(str(count)) * price
    return int(total.to_integral_value(rounding=ROUND_HALF_UP))


def _now():
    return datetime.now(VIETNAM)


class ClientUsage:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def _database(self):
        # The database can contain user prompts. Reserve it with owner-only access.
        fd = os.open(self.path, os.O_CREAT | os.O_CLOEXEC | os.O_WRONLY, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA secure_delete = ON")
            db.execute("""CREATE TABLE IF NOT EXISTS usage (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, model TEXT NOT NULL, kind TEXT NOT NULL,
                route TEXT NOT NULL, created_at REAL NOT NULL, status TEXT NOT NULL,
                started_at REAL, ended_at REAL, reference_count INTEGER NOT NULL,
                account_attempts TEXT NOT NULL DEFAULT '[]', settings TEXT NOT NULL,
                usage TEXT NOT NULL DEFAULT '{}', charge INTEGER, upstream INTEGER,
                error_code TEXT, error TEXT, input TEXT, output TEXT, asset TEXT, expires_at REAL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS usage_owner_time ON usage(owner, created_at)")
            db.execute("CREATE INDEX IF NOT EXISTS usage_time ON usage(created_at)")
            db.execute("CREATE INDEX IF NOT EXISTS usage_quota ON usage(owner, model, created_at)")
            db.execute("CREATE INDEX IF NOT EXISTS usage_pending ON usage(id) "
                       "WHERE status IN ('queued','processing')")
            yield db

    def begin(self, job_id, client, model, kind, route, *, prompt=None, references=0, parameters=None):
        now = _now()
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        rule = (client.get("rules") or {}).get(model, {})
        retention = client.get("history_days", 0)
        settings = {"charge": rule.get("charge", {}), "upstream": rule.get("upstream", {}),
                    "parameters": parameters or {}}
        try:
            with self._database() as db:
                db.execute("BEGIN IMMEDIATE")
                limit = rule.get("daily_limit")
                if limit is not None:
                    count = db.execute(
                        "SELECT count(*) FROM usage WHERE owner=? AND model=? AND created_at>=?",
                        (client["id"], model, day.timestamp()),
                    ).fetchone()[0]
                    if count >= limit:
                        retry = math.ceil((day + timedelta(days=1) - now).total_seconds())
                        raise HTTPException(429, {"code": "CLIENT_DAILY_LIMIT",
                                                  "message": "Mô hình đã đạt hạn mức ngày của khóa API."},
                                            headers={"Retry-After": str(retry)})
                db.execute(
                    """INSERT INTO usage (id, owner, model, kind, route, created_at, status,
                        reference_count, settings, input, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (job_id, client["id"], model, kind, route, now.timestamp(), "queued", references,
                     json.dumps(settings), redact_content(prompt) if retention else None,
                     (now + timedelta(days=retention)).timestamp() if retention else None),
                )
        except (OSError, sqlite3.Error) as exc:
            raise HTTPException(507, "Không lưu được biên nhận sử dụng API.") from exc
        private = client.get("rules") is not None or "history_days" in client
        return ClientJob(self, job_id, retention > 0, private)

    def discard(self, job_id):
        with self._database() as db:
            db.execute("DELETE FROM usage WHERE id=? AND status='queued'", (job_id,))

    def started(self, job_id):
        with self._database() as db:
            db.execute("UPDATE usage SET status='processing', started_at=? WHERE id=? AND status='queued'",
                       (time.time(), job_id))

    def attempt(self, job_id, account_id, label):
        with self._database() as db:
            db.execute(
                """UPDATE usage SET account_attempts=json_insert(account_attempts, '$[#]', json(?))
                   WHERE id=? AND status IN ('queued','processing')""",
                (json.dumps({"id": account_id, "label": label, "started_at": time.time()}), job_id),
            )

    def finish(self, job_id, status, *, usage=None, output=None, asset=None, code=None, error=None, ended_at=None):
        usage = usage or {}
        with self._database() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT settings, expires_at FROM usage WHERE id=? AND status IN ('queued','processing')",
                             (job_id,)).fetchone()
            if not row:
                return
            settings = json.loads(row["settings"])
            now = time.time()
            keep_content = (row["expires_at"] or 0) > now
            charge = _total_price(settings["charge"], usage) if status == "completed" else (
                None if status == "interrupted" else 0)
            upstream = _total_price(settings["upstream"], usage) if status == "completed" else None
            db.execute(
                """UPDATE usage SET status=?, ended_at=?, usage=?, charge=?, upstream=?,
                   error_code=?, error=?, output=?, asset=? WHERE id=?""",
                (status, ended_at or now, json.dumps(usage), charge, upstream, code, error,
                 redact_content(output) if keep_content else None, asset if keep_content else None, job_id),
            )

    def prune(self):
        with self._database() as db:
            db.execute("""UPDATE usage SET input=NULL, output=NULL, asset=NULL
                          WHERE expires_at<=? AND (input IS NOT NULL OR output IS NOT NULL OR asset IS NOT NULL)""", (time.time(),))

    def reconcile(self, tasks, *, restarting=False, resuming=()):
        with self._database() as db:
            pending = db.execute("SELECT id FROM usage WHERE status IN ('queued','processing')").fetchall()
        for row in pending:
            task = tasks.get(row["id"], {})
            status = task.get("status")
            ended_at = task.get("updated_at")
            # Let the worker finish the normal write first, including optional output text.
            if not restarting and ended_at and time.time() - ended_at < 5:
                continue
            if status == "completed":
                usage = task.get("usage") or ({"images": 1} if task.get("kind") == "image" else {})
                self.finish(row["id"], status, usage=usage, ended_at=ended_at,
                            asset=(task.get("result") or {}).get("filename"))
            elif status in ("failed", "cancelled", "interrupted"):
                self.finish(row["id"], status, code=task.get("error_code"),
                            error=redact_content(task.get("error")), ended_at=ended_at)
            elif restarting and row["id"] not in resuming:
                self.finish(row["id"], "interrupted", code="SERVICE_RESTARTED",
                            error="Dịch vụ đã khởi động lại. Cần đối chiếu kết quả.")

    def import_media(self, tasks, account_labels):
        rows = []
        for task in tasks:
            kind, owner, status = task.get("kind"), task.get("owner"), task.get("status")
            provider = task.get("provider") or "muse"
            if (not owner or task.get("receipt_only") or kind not in ("image", "video")
                    or status not in ("completed", "failed", "cancelled", "interrupted")):
                continue
            aid = task.get("account")
            created = task.get("created_at") or 0
            started = task.get("started_at")
            accounts = [{"id": aid, "label": account_labels.get(aid, aid), "started_at": started or created}] if aid else []
            usage = {"images": 1} if kind == "image" and status == "completed" else {}
            rows.append((task["id"], owner, f"{provider}/{kind}", kind,
                         "/v1/images/tasks" if kind == "image" else "/v1/videos",
                         created, status, started, task.get("updated_at") or created,
                         task.get("reference_count") or 0, json.dumps(accounts),
                         json.dumps({"charge": {}, "upstream": {}, "parameters": {}}),
                         json.dumps(usage), task.get("error_code")))
        if rows:
            with self._database() as db:
                db.executemany(
                    """INSERT OR IGNORE INTO usage (id, owner, model, kind, route, created_at, status,
                       started_at, ended_at, reference_count, account_attempts, settings, usage, error_code)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows)

    def _view(self, row, *, admin, detail=False):
        view = dict(row)
        settings = json.loads(view.pop("settings"))
        view["usage"] = json.loads(view["usage"])
        view["parameters"] = settings["parameters"]
        view["currency"] = "VND"
        view["queue_seconds"] = round(max(0, (view["started_at"] or view["ended_at"] or time.time())
                                              - view["created_at"]), 2)
        view["run_seconds"] = (round(max(0, (view["ended_at"] or time.time()) - view["started_at"]), 2)
                               if view["started_at"] else None)
        expires_at = view.pop("expires_at")
        if not detail or not expires_at or expires_at <= time.time():
            for key in ("input", "output", "asset"):
                view.pop(key, None)
        if admin:
            if detail:
                view["content_expires_at"] = expires_at
            view["account_attempts"] = json.loads(view["account_attempts"])
            view["upstream_basis"] = "estimated" if view["upstream"] is not None else "unknown"
            view["prices"] = {key: settings[key] for key in ("charge", "upstream")}
        else:
            for key in ("account_attempts", "upstream", "owner"):
                view.pop(key)
        return view

    def list(self, *, admin, owner=None, model=None, status=None, account=None,
             since=None, until=None, limit=50, offset=0):
        clauses, params = [], []
        for expr, value in (("owner=?", owner), ("model=?", model), ("status=?", status),
                            ("created_at>=?", since), ("created_at<?", until),
                            ("EXISTS (SELECT 1 FROM json_each(usage.account_attempts) a "
                             "WHERE json_extract(a.value,'$.id')=?)", account)):
            if value is not None:
                clauses.append(expr)
                params.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._database() as db:
            db.execute("BEGIN")
            totals = dict(db.execute(
                """SELECT count(*) AS requests, sum(charge) AS charge,
                          count(CASE WHEN status IN ('completed','interrupted') AND charge IS NULL THEN 1 END)
                              AS charge_unknown
                   FROM usage""" + where, params,
            ).fetchone())
            if admin:
                final_account = "json_extract(account_attempts, '$[#-1].id')"
                upstream = db.execute(
                    """SELECT sum(upstream) AS upstream,
                              sum(CASE WHEN status IN ('completed','failed','interrupted') AND upstream IS NULL
                                       THEN 1 ELSE 0 END) AS upstream_unknown FROM usage"""
                    + where + (f" AND {final_account}=?" if account is not None else ""),
                    [*params, account] if account is not None else params,
                ).fetchone()
                totals.update(dict(upstream))
            rows = db.execute(
                f"SELECT * FROM usage{where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (*params, limit + 1, offset),
            ).fetchall()
        return {"items": [self._view(row, admin=admin) for row in rows[:limit]],
                "has_more": len(rows) > limit, "totals": totals}

    def detail(self, job_id, *, admin, owner=None):
        with self._database() as db:
            row = db.execute("SELECT * FROM usage WHERE id=? AND (? IS NULL OR owner=?)",
                             (job_id, owner, owner)).fetchone()
        if not row:
            raise HTTPException(404, "Không tìm thấy lượt sử dụng API.")
        return self._view(row, admin=admin, detail=True)

    def overview(self, days):
        today = _now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        since = today - (days - 1) * 86400
        aggregates = """
            count(*) AS requests, sum(status='completed') AS completed,
            sum(status='failed') AS failed, sum(status='interrupted') AS interrupted,
            sum(status='cancelled') AS cancelled,
            sum(CASE WHEN status='completed' THEN json_extract(usage, '$.images') END) AS images,
            sum(CASE WHEN status='completed' THEN json_extract(usage, '$.video_seconds') END) AS video_seconds,
            sum(CASE WHEN status='completed' THEN json_extract(usage, '$.input_tokens') END) AS input_tokens,
            sum(CASE WHEN status='completed' THEN json_extract(usage, '$.output_tokens') END) AS output_tokens,
            sum(charge) AS charge, sum(upstream) AS upstream,
            sum(status IN ('completed','interrupted') AND charge IS NULL) AS charge_unknown,
            sum(status IN ('completed','failed','interrupted') AND upstream IS NULL) AS upstream_unknown
        """
        with self._database() as db:
            db.execute("BEGIN")
            totals = dict(db.execute(f"SELECT {aggregates} FROM usage WHERE created_at>=?", (since,)).fetchone())
            kinds = [dict(row) for row in db.execute(
                f"SELECT kind, {aggregates} FROM usage WHERE created_at>=? GROUP BY kind", (since,))]
            owners = {row["owner"]: dict(row) for row in db.execute(
                f"""SELECT owner, max(created_at) AS last_seen, {aggregates}
                    FROM usage WHERE created_at>=? GROUP BY owner""", (since,))}
            daily = {(row["owner"], row["model"]): row["requests"] for row in db.execute(
                """SELECT owner, model, count(*) AS requests FROM usage
                   WHERE created_at>=? GROUP BY owner, model""", (today,))}
            running = {row["owner"]: dict(row) for row in db.execute(
                """SELECT owner, sum(status='processing') AS processing, sum(status='queued') AS queued
                   FROM usage WHERE status IN ('processing','queued') GROUP BY owner""")}
            recent = [dict(row) for row in db.execute(
                """SELECT id, owner, model, kind, status, created_at FROM usage WHERE created_at>=?
                   ORDER BY created_at DESC, id DESC LIMIT 8""", (since,))]
        return {"totals": totals, "kinds": kinds, "owners": owners, "daily": daily,
                "running": running, "recent": recent, "today_count": sum(daily.values()),
                "since": since, "resets_at": today + 86400}

    def month(self):
        start = _now().replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        with self._database() as db:
            rows = db.execute(
                """SELECT owner, count(*) AS requests, sum(status='completed') AS completed,
                          sum(charge) AS charge, sum(upstream) AS upstream,
                          sum(status IN ('completed','interrupted') AND charge IS NULL) AS charge_unknown,
                          sum(status IN ('completed','failed','interrupted') AND upstream IS NULL) AS upstream_unknown, max(created_at) AS last_used
                   FROM usage WHERE created_at>=? GROUP BY owner""", (start,)
            ).fetchall()
        return {row["owner"]: dict(row) for row in rows}


class ClientJob:
    def __init__(self, ledger, job_id, capture, private=False):
        self.ledger = ledger
        self.id = job_id
        self.capture = capture
        self.private = private

    def record(self, method, *args, **kwargs):
        # Once admitted, a disk error must not retry the provider or discard its result.
        try:
            getattr(self.ledger, method)(self.id, *args, **kwargs)
        except (OSError, sqlite3.Error):
            log.exception("Không ghi được lịch sử sử dụng API %s", self.id)


class TextOutcome:
    """Summarize the same OpenAI-style JSON and SSE frames delivered to the caller."""

    def __init__(self, capture, report_tokens):
        self.capture = capture
        self.report_tokens = report_tokens
        self.usage = {}
        self.output = ""
        self.failure = None
        self.terminal = False
        self.line = b""
        self.skip_line = False

    def _add(self, text):
        if self.capture and isinstance(text, str):
            self.output += text[:max(0, CONTENT_LIMIT - len(self.output))]

    def observe(self, data):
        if not isinstance(data, dict):
            return
        if self.report_tokens:
            self.usage.update(usage_from_response(data))
        if error := data.get("error"):
            code = error.get("code") if isinstance(error, dict) else None
            self.failure = str(code or "CHAT_FAILED")
        event = data.get("type")
        if event == "response.output_text.delta":
            self._add(data.get("delta"))
        elif event in ("response.failed", "response.incomplete"):
            self.failure = self.failure or "CHAT_FAILED"
        elif event == "response.completed":
            self.terminal = True

        choices = data.get("choices")
        for choice in choices if isinstance(choices, list) else ():
            if not isinstance(choice, dict):
                continue
            reason = choice.get("finish_reason")
            if reason:
                self.terminal = True
            if reason in ("content_filter", "error"):
                self.failure = str(reason)
            msg = choice.get("message") or choice.get("delta")
            if isinstance(msg, dict):
                self._add(msg.get("content"))
                if isinstance(msg.get("tool_calls"), list) and self.capture:
                    self._add(json.dumps(msg["tool_calls"], ensure_ascii=False))
        output = data.get("output")
        if self.capture and isinstance(output, list):
            for item in output:
                if isinstance(item, dict):
                    for content in item.get("content") or ():
                        if isinstance(content, dict):
                            self._add(content.get("text"))

    def feed(self, chunk):
        raw = chunk.encode() if isinstance(chunk, str) else bytes(chunk)
        lines = raw.split(b"\n")
        for index, line in enumerate(lines):
            if len(self.line) + len(line) > 262144:
                self.skip_line = True
                self.line = b""
            if not self.skip_line:
                self.line += line
            if index == len(lines) - 1:
                break
            if self.line.startswith(b"data:"):
                payload = self.line[5:].strip()
                if payload == b"[DONE]":
                    self.terminal = True
                elif payload:
                    try:
                        self.observe(json.loads(payload))
                    except (UnicodeError, ValueError, TypeError, AttributeError):
                        pass
            self.line = b""
            self.skip_line = False

    def finish(self, job):
        if self.failure:
            uncertain = self.failure == "DELIVERY_UNKNOWN"
            job.record("finish", "interrupted" if uncertain else "failed",
                       code=self.failure[:100], error="Cần đối chiếu kết quả." if uncertain
                       else "AI báo lỗi khi trả nội dung.",
                       usage=self.usage, output=self.output)
        elif not self.terminal:
            job.record("finish", "interrupted", code="STREAM_INTERRUPTED",
                       error="Luồng phản hồi bị gián đoạn. Cần đối chiếu kết quả.",
                       usage=self.usage, output=self.output)
        else:
            job.record("finish", "completed", usage=self.usage, output=self.output)
