"""Operator CLI for console credentials.

    python -m chatmemory.admin.issue_token issue  <operator> [--label laptop]
    python -m chatmemory.admin.issue_token revoke <operator>
    python -m chatmemory.admin.issue_token list
    python -m chatmemory.admin.issue_token log [--limit 20]

Modelled on `mcp/issue_token.py`, with one difference that is the whole point
of the console: a credential names an **operator**, not a Discord account, and
the name it carries is what every change that credential makes will be
attributed to. Issue one per person. Two people sharing one puts the wrong
name in the record, and nothing technical can notice.

`issue` prints the credential once. It is stored only as a hash, so a lost
token is re-issued and the old one revoked, never recovered -- and revoking
one operator leaves every other operator working, which is what makes doing it
promptly realistic.

Issuing and revoking are themselves recorded, from this shell rather than from
a console session, because the first credential has to be issued by somebody
who does not hold one yet. `log` reads that record back without needing the
console to exist.
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from chatmemory.adapters.store.admin_postgres import (
    PostgresChangeRecord,
    PostgresOperatorTokens,
    grant_console_access,
    withdraw_console_access,
)
from chatmemory.admin.auth import Operator
from chatmemory.config import get_settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="admin-issue-token", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    issue = sub.add_parser("issue", help="mint a credential for one named operator")
    issue.add_argument("operator", help="the operator's name, e.g. 'ana'")
    issue.add_argument("--label", default="", help="where this credential lives")
    revoke = sub.add_parser("revoke", help="withdraw one operator's credentials")
    revoke.add_argument("operator")
    sub.add_parser("list", help="live credentials, hashes only")
    log = sub.add_parser("log", help="the most recent configuration changes")
    log.add_argument("--limit", type=int, default=20)
    return parser


async def _issue(engine: AsyncEngine, args: argparse.Namespace) -> None:
    issued = await grant_console_access(engine, Operator(args.operator), args.label)
    # Printed once, deliberately. Nothing stores it.
    print(f"{issued.operator}\n{issued.token}")


async def _revoke(engine: AsyncEngine, args: argparse.Namespace) -> None:
    operator = Operator(args.operator)
    revoked = await withdraw_console_access(engine, operator)
    print(f"revoked {revoked} credential(s) for {operator}")


async def _list(engine: AsyncEngine, _: argparse.Namespace) -> None:
    for record in await PostgresOperatorTokens(engine).active_tokens():
        print(f"{record.operator}  {record.token_hash[:12]}...  {record.label}")


async def _log(engine: AsyncEngine, args: argparse.Namespace) -> None:
    for entry in await PostgresChangeRecord(engine).recent(args.limit):
        print(
            f"{entry.recorded_at.isoformat()}  {entry.operator}  "
            f"[{entry.kind}]  {entry.setting}: "
            f"{entry.before!r} -> {entry.after!r}"
            + (f"  ({entry.reason})" if entry.reason else "")
        )


COMMANDS = {"issue": _issue, "revoke": _revoke, "list": _list, "log": _log}


async def run(args: argparse.Namespace) -> None:
    settings = get_settings()
    # Migration 0012 owns `admin_token` and `config_audit`. Failing here
    # against an unmigrated database is the point: an operator sees it
    # immediately, where a table quietly created by this CLI would drift from
    # the schema unnoticed and be absent from a restore.
    engine = create_async_engine(settings.database_url.get_secret_value())
    try:
        await COMMANDS[args.command](engine, args)
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(run(_parser().parse_args()))


if __name__ == "__main__":
    main()
