"""Transactional run/document storage. PostgreSQL in deployment, SQLite for local tests."""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import (create_engine, MetaData, Table, Column, String, DateTime,
                        LargeBinary, Integer, Text, ForeignKey, select, insert, update, text)

ROOT = Path(__file__).parent
raw_database_url = os.getenv("DATABASE_URL", "").strip()
if raw_database_url:
    parsed_url = urlparse(raw_database_url)
    if (parsed_url.hostname or "").upper() in {"HOST", "YOUR_HOST", "DATABASE_HOST"}:
        raise RuntimeError(
            "DATABASE_URL still contains a placeholder host. For local use, run "
            "Remove-Item Env:DATABASE_URL and start the dashboard again; "
            "for PostgreSQL, replace HOST with the real database hostname."
        )
url = raw_database_url or "sqlite:///" + str(ROOT / "asa_local.db").replace("\\", "/")
if url.startswith("postgres://"):
    url = "postgresql://" + url[len("postgres://"):]
if url.startswith("postgresql://"):
    url = url.replace("postgresql://", "postgresql+psycopg://", 1)
engine = create_engine(url, pool_pre_ping=True, future=True)
decision_lock = threading.Lock()
meta = MetaData()
migrations = Table("schema_migrations", meta, Column("version", Integer, primary_key=True))
documents = Table("documents", meta, Column("id", String(36), primary_key=True),
                  Column("filename", String(180), nullable=False), Column("mime", String(80), nullable=False),
                  Column("content", LargeBinary, nullable=False), Column("created_at", DateTime(timezone=True), nullable=False))
runs = Table("processing_runs", meta, Column("id", String(36), primary_key=True),
             Column("document_id", String(36), ForeignKey("documents.id")),
             Column("created_at", DateTime(timezone=True), nullable=False),
             Column("source", String(180), nullable=False), Column("event_type", String(40), nullable=False),
             Column("status", String(30), nullable=False), Column("payload", Text, nullable=False))
stages = Table("processing_stages", meta, Column("id", Integer, primary_key=True, autoincrement=True),
               Column("run_id", String(36), ForeignKey("processing_runs.id"), nullable=False),
               Column("stage", String(50), nullable=False), Column("status", String(15), nullable=False),
               Column("detail", Text, nullable=False), Column("at", DateTime(timezone=True), nullable=False))
reviews = Table("manual_reviews", meta, Column("id", String(36), primary_key=True),
                Column("run_id", String(36), ForeignKey("processing_runs.id"), nullable=False),
                Column("reviewer", String(80), nullable=False), Column("outcome", String(30), nullable=False),
                Column("note", Text, nullable=False), Column("corrections", Text, nullable=False),
                Column("revised_decision", Text), Column("at", DateTime(timezone=True), nullable=False))
ocr_cache = Table("ocr_cache", meta, Column("key", String(100), primary_key=True),
                  Column("text", Text, nullable=False), Column("method", String(20), nullable=False),
                  Column("pages", Integer, nullable=False), Column("created_at", DateTime(timezone=True), nullable=False))

def now(): return datetime.now(timezone.utc)

def initialize():
    # Version 1 is additive and leaves any imported legacy JSON untouched.
    with engine.begin() as conn:
        meta.create_all(conn)
        if conn.execute(select(migrations.c.version)).first() is None:
            conn.execute(insert(migrations).values(version=1))

def recover_interrupted():
    """Close runs left active by a terminated single-worker process."""
    with engine.begin() as conn:
        for row in conn.execute(select(runs).where(runs.c.status == "RUNNING")).mappings():
            payload = json.loads(row["payload"] or "{}")
            payload.update(error="Worker stopped before processing completed",
                           reasons=["Processing interrupted; resubmit for review"])
            conn.execute(update(runs).where(runs.c.id == row["id"]).values(
                status="FAILED", payload=json.dumps(payload)))
            conn.execute(insert(stages).values(run_id=row["id"], stage="Processing interrupted", status="FAILED",
                                               detail="Worker stopped before processing completed", at=now()))

def create_run(source, event_type, document=None, mime=None):
    rid = str(uuid.uuid4()); did = str(uuid.uuid4()) if document is not None else None
    with engine.begin() as conn:
        if did:
            conn.execute(insert(documents).values(id=did, filename=source, mime=mime, content=document, created_at=now()))
        conn.execute(insert(runs).values(id=rid, document_id=did, created_at=now(), source=source,
                                         event_type=event_type, status="RUNNING", payload="{}"))
    return rid

def cached_ocr(key):
    with engine.connect() as conn:
        row = conn.execute(select(ocr_cache.c.text, ocr_cache.c.method, ocr_cache.c.pages)
                           .where(ocr_cache.c.key == key)).first()
        return tuple(row) if row else None

def save_ocr(key, value):
    from sqlalchemy.exc import IntegrityError
    try:
        with engine.begin() as conn:
            conn.execute(insert(ocr_cache).values(key=key, text=value[0], method=value[1],
                                                  pages=value[2], created_at=now()))
    except IntegrityError:
        pass  # A concurrent run stored the same document first.

def add_stage(rid, stage, status, detail=""):
    with engine.begin() as conn:
        conn.execute(insert(stages).values(run_id=rid, stage=stage, status=status, detail=detail, at=now()))

def finish_run(rid, payload, status):
    with engine.begin() as conn:
        conn.execute(update(runs).where(runs.c.id == rid).values(payload=json.dumps(payload, default=str), status=status))

def _row(row):
    value = dict(row)
    payload = json.loads(value.pop("payload") or "{}")
    value.update(payload)
    value["run_id"] = value.pop("id")
    value["time"] = value.pop("created_at").isoformat()
    return value

def get_run(rid):
    with engine.connect() as conn:
        row = conn.execute(select(runs).where(runs.c.id == rid)).mappings().first()
        if not row: return None
        result = _row(row)
        result["stages"] = [dict(s) for s in conn.execute(select(stages.c.stage, stages.c.status, stages.c.detail, stages.c.at)
                                                       .where(stages.c.run_id == rid).order_by(stages.c.id)).mappings()]
        result["reviews"] = [dict(r) for r in conn.execute(select(reviews.c.reviewer, reviews.c.outcome, reviews.c.note,
                                                                    reviews.c.corrections, reviews.c.revised_decision, reviews.c.at)
                                                        .where(reviews.c.run_id == rid).order_by(reviews.c.at)).mappings()]
        return result

def list_runs(limit=300, include_archived=False):
    with engine.connect() as conn:
        rows = [_row(r) for r in conn.execute(select(runs).order_by(runs.c.created_at.desc()).limit(limit)).mappings()]
        return rows if include_archived else [r for r in rows if not r.get("archived")]

def archive_history():
    """Hide completed runs from the dashboard while retaining the immutable AP ledger."""
    count = 0
    with engine.begin() as conn:
        for row in conn.execute(select(runs).where(runs.c.status != "RUNNING")).mappings():
            payload = json.loads(row["payload"] or "{}")
            if payload.get("archived"): continue
            payload["archived"] = True
            conn.execute(update(runs).where(runs.c.id == row["id"]).values(payload=json.dumps(payload)))
            count += 1
    return count

def document_for_run(rid):
    with engine.connect() as conn:
        return conn.execute(select(documents.c.content, documents.c.mime, documents.c.filename)
                            .join(runs, runs.c.document_id == documents.c.id).where(runs.c.id == rid)).first()

def record_review(rid, reviewer, outcome, note, corrections, revised_decision=None):
    with engine.begin() as conn:
        if not conn.execute(select(runs.c.id).where(runs.c.id == rid)).first(): raise ValueError("Run not found")
        conn.execute(insert(reviews).values(id=str(uuid.uuid4()), run_id=rid, reviewer=reviewer, outcome=outcome,
                                            note=note, corrections=json.dumps(corrections),
                                            revised_decision=json.dumps(revised_decision, default=str) if revised_decision else None,
                                            at=now()))

def locked_decision(rid, invoice, po_path, method, extra=None, finalize=None):
    """Serialize PO consumption and duplicate decisions with the run commit."""
    from asa_invoice_processor import decide
    with decision_lock:
        with engine.begin() as conn:
            if engine.dialect.name == "postgresql": conn.execute(text("SELECT pg_advisory_xact_lock(9183415)"))
            history = [_row(r) for r in conn.execute(select(runs).where(runs.c.status != "RUNNING")).mappings()]
            decision = decide(invoice, po_path, method=method, history_rows=history)
            final_extra, final_stages = finalize(decision) if finalize else ({}, [])
            payload = {"invoice": decision.invoice, "invoice_number": invoice.get("invoice_number"),
                       "vendor": invoice.get("vendor"), "gstin": invoice.get("gstin"),
                       "po_number": invoice.get("po_number"), "total": invoice.get("total"),
                       "due_date": (invoice.get("details") or {}).get("due_date"),
                       "document_type": decision.document_type, "method": method,
                       "decision": decision.__dict__, "reasons": decision.reasons,
                       **(extra or {}), **final_extra}
            for name, status, detail in final_stages:
                conn.execute(insert(stages).values(run_id=rid, stage=name, status=status,
                                                   detail=detail, at=now()))
            conn.execute(update(runs).where(runs.c.id == rid).values(payload=json.dumps(payload, default=str), status=decision.status))
            return decision

def import_legacy(path, manual_path=None):
    """Idempotent optional import, invoked explicitly by the migration command."""
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    manual = json.loads(Path(manual_path).read_text(encoding="utf-8")) if manual_path and Path(manual_path).exists() else {}
    with engine.begin() as conn:
        for item in rows:
            rid = str(item.get("run_id") or uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(item, sort_keys=True)))
            if conn.execute(select(runs.c.id).where(runs.c.id == rid)).first(): continue
            payload = {k:v for k,v in item.items() if k not in ("run_id", "time", "source", "status", "event_type")}
            if rid in manual: payload["legacy_manual_check"] = manual[rid]
            try: created = datetime.fromisoformat(item["time"])
            except Exception: created = now()
            conn.execute(insert(runs).values(id=rid, document_id=None, created_at=created,
                                             source=item.get("source") or item.get("scenario") or "Legacy run",
                                             event_type=item.get("event_type") or "legacy",
                                             status=item.get("status") or "PENDING_REVIEW", payload=json.dumps(payload)))
