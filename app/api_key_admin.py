from __future__ import annotations

"""Local operator commands for API consumers and one-time credentials."""

import argparse
import getpass
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import TextIO

from . import config
from .api_auth import (
    ApiAuthError, create_consumer, issue_api_key, revoke_api_key, revoke_consumer,
)
from .database import get_db
from .db_admin import verify_database


def _expiry(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expiry must be an ISO 8601 timestamp with timezone") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise argparse.ArgumentTypeError("expiry must include a timezone")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage InfoHub API keys on the database host")
    actions = parser.add_subparsers(dest="action", required=True)
    create = actions.add_parser("consumer-create")
    create.add_argument("name")
    actions.add_parser("consumer-list")
    revoke_consumer_command = actions.add_parser("consumer-revoke")
    revoke_consumer_command.add_argument("consumer_id")
    issue = actions.add_parser("key-issue")
    issue.add_argument("consumer_id")
    issue.add_argument("--scope", action="append", required=True)
    issue.add_argument("--expires-at", type=_expiry, required=True)
    keys = actions.add_parser("key-list")
    keys.add_argument("consumer_id")
    revoke_key_command = actions.add_parser("key-revoke")
    revoke_key_command.add_argument("key_id")
    audit = actions.add_parser("audit-list")
    audit.add_argument("--consumer-id")
    audit.add_argument("--limit", type=int, default=50)
    request_audit = actions.add_parser("request-audit-list")
    request_audit.add_argument("--consumer-id")
    request_audit.add_argument("--key-id")
    request_audit.add_argument("--limit", type=int, default=50)
    return parser


def main(argv: list[str] | None = None, *, db_path: Path | None = None,
         output: TextIO | None = None, actor: str | None = None) -> int:
    args = _parser().parse_args(argv)
    target = Path(db_path or config.DB_PATH)
    stream = output or sys.stdout
    operator = actor or f"local-cli:{getpass.getuser()}"
    verify_database(target, require_current=True)

    try:
        with get_db(target) as db:
            if args.action == "consumer-create":
                result = {"consumer_id": create_consumer(db, args.name, actor=operator)}
            elif args.action == "consumer-list":
                result = [dict(row) for row in db.execute(
                    """SELECT id,name,status,authz_version,created_at,revoked_at
                       FROM api_consumers ORDER BY created_at,id""")]
            elif args.action == "consumer-revoke":
                result = {"revoked": revoke_consumer(db, args.consumer_id, actor=operator)}
            elif args.action == "key-issue":
                issued = issue_api_key(
                    db, args.consumer_id, set(args.scope),
                    expires_at=args.expires_at, actor=operator,
                )
                result = {"key_id": issued.key_id, "consumer_id": issued.consumer_id,
                          "scopes": issued.scopes, "expires_at": issued.expires_at,
                          "token": issued.token}
            elif args.action == "key-list":
                result = [dict(row) for row in db.execute(
                    """SELECT key_id,consumer_id,scopes_json,issued_at,expires_at,revoked_at
                       FROM api_keys WHERE consumer_id=? ORDER BY issued_at,key_id""",
                    (args.consumer_id,),
                )]
            elif args.action == "key-revoke":
                result = {"revoked": revoke_api_key(db, args.key_id, actor=operator)}
            elif args.action == "audit-list":
                if not 1 <= args.limit <= 1000:
                    raise ApiAuthError("audit limit must be between 1 and 1000")
                sql = """SELECT id,action,actor,consumer_id,key_id,details_json,occurred_at
                         FROM api_key_audit"""
                parameters: tuple = ()
                if args.consumer_id:
                    sql += " WHERE consumer_id=?"
                    parameters = (args.consumer_id,)
                sql += " ORDER BY id DESC LIMIT ?"
                result = [dict(row) for row in db.execute(sql, (*parameters, args.limit))]
            else:
                if not 1 <= args.limit <= 1000:
                    raise ApiAuthError("audit limit must be between 1 and 1000")
                filters, parameters = [], []
                if args.consumer_id:
                    filters.append("consumer_id=?")
                    parameters.append(args.consumer_id)
                if args.key_id:
                    filters.append("key_id=?")
                    parameters.append(args.key_id)
                sql = """SELECT id,request_id,event,consumer_id,key_id,method,resource,
                                required_scope,status_code,error_code,duration_ms,occurred_at
                         FROM api_request_audit"""
                if filters:
                    sql += " WHERE " + " AND ".join(filters)
                sql += " ORDER BY id DESC LIMIT ?"
                parameters.append(args.limit)
                result = [dict(row) for row in db.execute(sql, tuple(parameters))]
    except ApiAuthError as exc:
        print(f"API key operation rejected: {exc}", file=sys.stderr)
        return 2
    # The one-time token is emitted only here, after its transaction commits.
    print(json.dumps(result, ensure_ascii=False), file=stream)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
