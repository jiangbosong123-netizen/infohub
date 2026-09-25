from __future__ import annotations

"""Loopback-only, one-decision-at-a-time topic sample review console."""

import hmac
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import config
from .database import get_db
from .topic_assignment_reviews import (
    TopicAssignmentReviewError,
    record_topic_assignment_review,
)
from .topic_review_sampling import get_sample_batch, sample_queue, sample_report


templates = Jinja2Templates(
    directory=str(config.BASE_DIR / "app" / "web" / "templates")
)


def _safe_source_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else None


def _sample_member(
    db: sqlite3.Connection, batch_id: str, assignment_id: str
):
    row = db.execute(
        """SELECT ordinal FROM topic_review_sampling_members
           WHERE batch_id=? AND assignment_id=?""",
        (batch_id, assignment_id),
    ).fetchone()
    if row is None:
        return None
    items = sample_queue(
        db, batch_id, after_ordinal=row["ordinal"] - 1,
        limit=1, pending_only=False,
    )
    if not items or items[0].assignment_id != assignment_id:
        raise RuntimeError("sample membership and queue projection disagree")
    return items[0]


def create_topic_review_console(
    *,
    batch_id: str,
    csrf_token: str,
    reviewer_id: str,
    db_path: str | Path | None = None,
) -> FastAPI:
    """Create a dedicated local app. It is never mounted by the production portal."""
    if not batch_id.strip():
        raise ValueError("batch_id must be non-empty")
    if not csrf_token:
        raise ValueError("csrf_token must be non-empty")
    reviewer_id = reviewer_id.strip()
    if not reviewer_id or len(reviewer_id) > 120:
        raise ValueError("reviewer_id must contain 1 to 120 characters")
    database_path = Path(db_path) if db_path is not None else config.DB_PATH

    app = FastAPI(
        title="InfoHub topic review console",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "testserver"],
    )

    @app.middleware("http")
    async def local_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/", response_class=HTMLResponse)
    def review_page(request: Request):
        with get_db(database_path) as db:
            batch = get_sample_batch(db, batch_id)
            report = sample_report(db, batch_id)
            pending = sample_queue(db, batch_id, limit=1, pending_only=True)
        item = pending[0] if pending else None
        return templates.TemplateResponse(
            request=request,
            name="topic_review_console.html",
            context={
                "batch": batch,
                "report": report,
                "item": item,
                "source_url": _safe_source_url(item.canonical_url) if item else None,
                "csrf_token": csrf_token,
                "reviewer_id": reviewer_id,
            },
        )

    @app.post("/review/{assignment_id}")
    async def submit_review(assignment_id: str, request: Request):
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
        if content_type != "application/x-www-form-urlencoded":
            raise HTTPException(status_code=415, detail="form encoding required")
        try:
            declared_length = int(request.headers.get("content-length", "0"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid content length") from exc
        if declared_length < 0 or declared_length > 8192:
            raise HTTPException(status_code=413, detail="form is too large")
        body = await request.body()
        if len(body) > 8192:
            raise HTTPException(status_code=413, detail="form is too large")
        try:
            form = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail="form must be UTF-8") from exc

        def one(name: str) -> str:
            values = form.get(name, [])
            if len(values) != 1:
                raise HTTPException(status_code=400, detail=f"exactly one {name} is required")
            return values[0]

        if not hmac.compare_digest(one("csrf_token"), csrf_token):
            raise HTTPException(status_code=403, detail="invalid CSRF token")
        decision = one("decision")
        reason = one("reason")
        expected = one("expected_previous_review_id") or None
        try:
            with get_db(database_path) as db:
                db.execute("BEGIN IMMEDIATE")
                item = _sample_member(db, batch_id, assignment_id)
                if item is None:
                    raise HTTPException(status_code=404, detail="assignment is not in this batch")
                record_topic_assignment_review(
                    db,
                    assignment_id=assignment_id,
                    decision=decision,
                    expected_previous_review_id=expected,
                    reviewer_id=reviewer_id,
                    reason=reason,
                )
        except TopicAssignmentReviewError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return RedirectResponse(url="/", status_code=303)

    return app
