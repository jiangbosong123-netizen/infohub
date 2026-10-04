from __future__ import annotations

"""Loopback-only console for blind owner relevance labels (D23 single-owner-v1).

The console shows exactly the frozen source content of each case (source title and
connector excerpt) after re-verifying its hash against the read-only legacy database. It
never reads or displays model fields (score, AI summary, translation, category, tmt), so a
batch it exports can honestly attest ``blind=true``. Labels go to an append-only private
draft; ``export`` turns the latest draft label per case into an ``owner-label-batch-v1``
batch for ``app.owner_label_intake``.
"""

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .evaluation import (
    OWNER_RECHECK_MIN_GAP,
    _parse_cases,
    _time,
    owner_protocol,
    owner_recheck_sample,
    validate_evaluation_dataset,
)
from .evaluation_sampling import legacy_content_sha256, legacy_item_content
from .review_intake import OWNER_BATCH_VERSION, OWNER_RECHECK_BATCH_VERSION

TASK = "relevance"
LABEL_DEFINITION = "relevance-definition-v1"
LABELS = ("relevant", "not_relevant", "unknown")
DRAFT_VERSION = "owner-label-draft-v1"
DRAFT_FIELDS = frozenset({
    "draft_version", "dataset_cases_sha256", "case_id", "content_sha256", "recorded_at", "label",
})
# Label the evaluation split first so an interrupted session still yields a usable test set.
SPLIT_ORDER = {"test": 0, "dev": 1, "train": 2, "security": 3}
OBJECT_REF = re.compile(r"private-db:items/([1-9][0-9]*)")
MAX_FORM_BYTES = 4096

templates = Jinja2Templates(directory=str(Path(__file__).parent / "web" / "templates"))


class OwnerConsoleError(RuntimeError):
    pass


@dataclass(frozen=True)
class FrozenContent:
    case_id: str
    content_sha256: str
    title: str
    text: str
    language: str
    source_name: str
    published_at: str | None


def _require_private(path: Path, what: str) -> None:
    repository = Path(__file__).parents[1].resolve()
    resolved = path.resolve()
    private_root = repository / "evaluation" / "private"
    if resolved.is_relative_to(repository) and not resolved.is_relative_to(private_root):
        raise OwnerConsoleError(f"{what} must be outside the repository or under evaluation/private")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class OwnerLabelSession:
    def __init__(
        self,
        dataset: Path | str,
        database: Path | str,
        draft_dir: Path | str,
        *,
        owner_id: str,
        order_seed: str = "owner-label-order-v1",
        mode: str = "label",
        now: datetime | None = None,
    ) -> None:
        if mode not in {"label", "recheck"}:
            raise OwnerConsoleError("mode must be label or recheck")
        self.mode = mode
        self.dataset = Path(dataset)
        self.database = Path(database)
        self.draft_dir = Path(draft_dir)
        self.owner_id = owner_id.strip()
        if not self.owner_id or len(self.owner_id) > 120:
            raise OwnerConsoleError("owner_id must contain 1 to 120 characters")
        if not self.database.is_file():
            raise OwnerConsoleError("database does not exist")
        report = validate_evaluation_dataset(self.dataset)
        self.dataset_version = report.dataset_version
        manifest_bytes = (self.dataset / "manifest.json").read_bytes()
        cases_bytes = (self.dataset / "cases.jsonl").read_bytes()
        self.manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        self.cases_sha256 = hashlib.sha256(cases_bytes).hexdigest()
        protocol = owner_protocol(json.loads(manifest_bytes))
        if protocol is not None and (
            protocol["owner_id"] != self.owner_id or protocol["label_definition"] != LABEL_DEFINITION
        ):
            raise OwnerConsoleError("dataset belongs to another owner or label definition")
        all_cases = _parse_cases(cases_bytes.decode("utf-8"))
        legacy = [
            case for case in all_cases
            if case.get("text_storage") == "restricted_reference"
            and OBJECT_REF.fullmatch(str(case.get("object_ref") or ""))
        ]
        if mode == "label":
            cases = [case for case in legacy if case["annotation"]["state"] == "unlabeled"]
            if not cases:
                raise OwnerConsoleError("dataset has no unlabeled restricted legacy-item cases")
            cases.sort(key=lambda case: (
                SPLIT_ORDER.get(case["split"], len(SPLIT_ORDER)),
                hashlib.sha256(f"{order_seed}:{case['case_id']}".encode("utf-8")).hexdigest(),
            ))
        else:
            if protocol is None:
                raise OwnerConsoleError("recheck requires an owner-labeled dataset")
            moment = now or datetime.now(timezone.utc)
            by_case = {case["case_id"]: case for case in legacy}
            # Only sampled first labels at least seven days old; the first label is never shown.
            cases = [
                by_case[case_id] for case_id in owner_recheck_sample(all_cases)
                if case_id in by_case
                and by_case[case_id]["annotation"]["state"] == "owner_labeled"
                and "owner_recheck" not in by_case[case_id]["annotation"]
                and moment - _time(by_case[case_id]["annotation"]["owner_label"]["recorded_at"], case_id)
                >= OWNER_RECHECK_MIN_GAP
            ]
            if not cases:
                raise OwnerConsoleError("no sampled case is due for a recheck yet")
        self.cases = cases
        self.by_id = {case["case_id"]: case for case in cases}
        self.order = [case["case_id"] for case in cases]
        _require_private(self.draft_dir, "draft directory")
        self.draft_dir.mkdir(exist_ok=True)
        suffix = "draft" if mode == "label" else "recheck.draft"
        self.draft_path = self.draft_dir / f"{self.dataset_version}.{self.cases_sha256[:16]}.{suffix}.jsonl"

    def content(self, case_id: str) -> FrozenContent:
        case = self.by_id.get(case_id)
        if case is None:
            raise OwnerConsoleError("case is not an unlabeled case of this dataset")
        item_id = int(OBJECT_REF.fullmatch(case["object_ref"]).group(1))
        db = sqlite3.connect(f"file:{self.database.resolve()}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            # Only source columns are selected; model fields never leave the database.
            row = db.execute(
                """SELECT item.title,item.raw_summary,item.published_at,source.name AS source_name
                   FROM items AS item JOIN sources AS source ON source.id=item.source_id
                   WHERE item.id=?""",
                (item_id,),
            ).fetchone()
        finally:
            db.close()
        if row is None:
            raise OwnerConsoleError(f"case {case_id} source item no longer exists")
        frozen = legacy_item_content(row)
        if legacy_content_sha256(frozen) != case["content_sha256"]:
            raise OwnerConsoleError(
                f"case {case_id} source content changed since sampling; stop and re-sample"
            )
        return FrozenContent(
            case_id=case_id,
            content_sha256=case["content_sha256"],
            title=frozen["title"],
            text=frozen["text"],
            language=case["language"],
            source_name=str(row["source_name"] or ""),
            published_at=row["published_at"],
        )

    def drafts(self) -> dict[str, dict]:
        latest: dict[str, dict] = {}
        if not self.draft_path.exists():
            return latest
        for number, line in enumerate(self.draft_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise OwnerConsoleError(f"draft line {number} is not valid JSON") from exc
            if (
                not isinstance(row, dict) or set(row) != DRAFT_FIELDS
                or row["draft_version"] != DRAFT_VERSION
                or row["dataset_cases_sha256"] != self.cases_sha256
                or row["case_id"] not in self.by_id
                or row["content_sha256"] != self.by_id[row["case_id"]]["content_sha256"]
                or row["label"] not in LABELS
            ):
                raise OwnerConsoleError(f"draft line {number} does not belong to this dataset")
            latest[row["case_id"]] = row
        return latest

    def record(self, case_id: str, label: str, content_sha256: str) -> None:
        if label not in LABELS:
            raise OwnerConsoleError("label must be relevant, not_relevant or unknown")
        case = self.by_id.get(case_id)
        if case is None or not hmac.compare_digest(content_sha256, case["content_sha256"]):
            raise OwnerConsoleError("form does not match the frozen case")
        self.content(case_id)  # re-verify the source before accepting a judgment about it
        row = {
            "draft_version": DRAFT_VERSION,
            "dataset_cases_sha256": self.cases_sha256,
            "case_id": case_id,
            "content_sha256": case["content_sha256"],
            "recorded_at": _now(),
            "label": label,
        }
        with self.draft_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def next_case(self, after: str | None = None) -> str | None:
        drafted = self.drafts()
        start = self.order.index(after) + 1 if after in self.by_id else 0
        for case_id in self.order[start:] + self.order[:start]:
            if case_id not in drafted:
                return case_id
        return None

    def neighbours(self, case_id: str) -> tuple[str | None, str | None]:
        index = self.order.index(case_id)
        previous = self.order[index - 1] if index > 0 else None
        following = self.order[index + 1] if index + 1 < len(self.order) else None
        return previous, following

    def progress(self) -> dict:
        drafted = self.drafts()
        by_split: dict[str, dict[str, int]] = {}
        for case in self.cases:
            counts = by_split.setdefault(case["split"], {"labeled": 0, "total": 0})
            counts["total"] += 1
            counts["labeled"] += int(case["case_id"] in drafted)
        labels = {label: 0 for label in LABELS}
        for row in drafted.values():
            labels[row["label"]] += 1
        return {
            "total": len(self.cases),
            "labeled": len(drafted),
            "labels": labels,
            "splits": dict(sorted(by_split.items(), key=lambda pair: SPLIT_ORDER.get(pair[0], 9))),
        }

    def export(self, output: Path | str) -> dict:
        output = Path(output)
        _require_private(output, "batch output")
        if output.exists():
            raise OwnerConsoleError("batch output already exists")
        if not output.parent.is_dir():
            raise OwnerConsoleError("batch output parent directory must already exist")
        drafted = self.drafts()
        if not drafted:
            raise OwnerConsoleError("draft has no labels to export")
        for case_id in drafted:
            self.content(case_id)
        manifest = {
            "schema_version": OWNER_BATCH_VERSION if self.mode == "label" else OWNER_RECHECK_BATCH_VERSION,
            "task": TASK,
            "label_definition": LABEL_DEFINITION,
            "source_dataset_version": self.dataset_version,
            "source_manifest_sha256": self.manifest_sha256,
            "source_cases_sha256": self.cases_sha256,
            "owner_id": self.owner_id,
            "source": "human",
            "blind": True,
            "model_assistance": False,
        }
        rows = [
            {
                "case_id": case_id,
                "content_sha256": drafted[case_id]["content_sha256"],
                "recorded_at": drafted[case_id]["recorded_at"],
                "labels": {"relevance": drafted[case_id]["label"]},
            }
            for case_id in self.order if case_id in drafted
        ]
        staging = Path(tempfile.mkdtemp(prefix="infohub-owner-batch-", dir=output.parent))
        try:
            (staging / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            (staging / "reviews.jsonl").write_text(
                "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
                encoding="utf-8",
            )
            os.rename(staging, output)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return {"batch": str(output), "exported_labels": len(rows), "progress": self.progress()}


def create_owner_label_console(session: OwnerLabelSession, *, csrf_token: str) -> FastAPI:
    """Create a dedicated local app. It is never mounted by the production portal."""
    if not csrf_token:
        raise ValueError("csrf_token must be non-empty")
    app = FastAPI(title="InfoHub owner labeling", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def local_security_headers(request: Request, call_next):
        request.state.nonce = secrets.token_urlsafe(16)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{request.state.nonce}'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def page(request: Request, **context) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="owner_label_console.html",
            context={
                "nonce": request.state.nonce,
                "csrf_token": csrf_token,
                "owner_id": session.owner_id,
                "dataset_version": session.dataset_version,
                "definition": LABEL_DEFINITION,
                "mode": session.mode,
                "labels": LABELS,
                "progress": session.progress(),
                **context,
            },
        )

    @app.get("/", response_class=HTMLResponse)
    def start(request: Request):
        case_id = session.next_case()
        if case_id is None:
            return page(request, case=None, error=None)
        return RedirectResponse(url=f"/case/{case_id}", status_code=303)

    @app.get("/case/{case_id}", response_class=HTMLResponse)
    def show(case_id: str, request: Request):
        if case_id not in session.by_id:
            raise HTTPException(status_code=404, detail="unknown case")
        try:
            content = session.content(case_id)
        except OwnerConsoleError as exc:
            return page(request, case=None, error=str(exc))
        previous, following = session.neighbours(case_id)
        current = session.drafts().get(case_id)
        return page(
            request, case=content, error=None, previous=previous, following=following,
            current=current["label"] if current else None,
            position=session.order.index(case_id) + 1,
        )

    @app.post("/label/{case_id}")
    async def label(case_id: str, request: Request):
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
        if content_type != "application/x-www-form-urlencoded":
            raise HTTPException(status_code=415, detail="form encoding required")
        body = await request.body()
        if len(body) > MAX_FORM_BYTES:
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
        try:
            session.record(case_id, one("label"), one("content_sha256"))
        except OwnerConsoleError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        following = session.next_case(after=case_id)
        return RedirectResponse(url=f"/case/{following}" if following else "/", status_code=303)

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Blind owner relevance labeling (D23)")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "export", "status"):
        command = commands.add_parser(name)
        command.add_argument("--recheck", action="store_true",
                             help="delayed blind relabel of the D23 recheck sample")
        command.add_argument("--dataset", required=True, type=Path)
        command.add_argument("--database", required=True, type=Path)
        command.add_argument("--draft-dir", required=True, type=Path)
        command.add_argument("--owner-id", required=True)
        if name == "serve":
            command.add_argument("--port", type=int, default=8013)
        if name == "export":
            command.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    session = OwnerLabelSession(
        args.dataset, args.database, args.draft_dir, owner_id=args.owner_id,
        mode="recheck" if args.recheck else "label",
    )
    if args.command == "status":
        print(json.dumps(session.progress(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "export":
        print(json.dumps(session.export(args.output), ensure_ascii=False, indent=2))
        return 0
    if not 1 <= args.port <= 65535:
        raise SystemExit("port must be between 1 and 65535")
    import uvicorn

    app = create_owner_label_console(session, csrf_token=secrets.token_urlsafe(32))
    print(f"本机标注台：http://127.0.0.1:{args.port}/  ·  数据集 {session.dataset_version}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
