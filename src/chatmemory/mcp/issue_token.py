"""Operator CLI for the token -> person mapping.

    python -m chatmemory.mcp.issue_token issue   <discord_user_id> [--label laptop]
    python -m chatmemory.mcp.issue_token rotate  <discord_user_id> [--label laptop]
    python -m chatmemory.mcp.issue_token revoke  <discord_user_id>
    python -m chatmemory.mcp.issue_token list

`issue` and `rotate` print the credential once. It is stored only as a hash,
so a lost token is rotated, never recovered -- and `rotate` touches nobody
else's credential, which is what makes rotating one routine enough to happen.
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy.ext.asyncio import create_async_engine

from chatmemory.config import get_settings
from chatmemory.domain.identity import PersonRef
from chatmemory.mcp.auth import PLATFORM, PostgresTokenStore, ensure_token_schema


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="issue_token", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("issue", "rotate"):
        cmd = sub.add_parser(name)
        cmd.add_argument("user_id", type=int, help="Discord user id")
        cmd.add_argument("--label", default="", help="where this token lives")
    revoke = sub.add_parser("revoke")
    revoke.add_argument("user_id", type=int)
    sub.add_parser("list")
    return parser


async def run(args: argparse.Namespace) -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url.get_secret_value())
    await ensure_token_schema(engine)
    store = PostgresTokenStore(engine)
    try:
        if args.command == "list":
            for record in await store.active_tokens():
                print(f"{record.person}  {record.token_hash[:12]}...  {record.label}")
            return

        person = PersonRef(PLATFORM, args.user_id)
        if args.command == "revoke":
            print(f"revoked {await store.revoke(person)} token(s) for {person}")
            return

        issued = (
            await store.rotate(person, args.label)
            if args.command == "rotate"
            else await store.issue(person, args.label)
        )
        # Printed once, deliberately. Nothing stores it.
        print(f"{issued.person}\n{issued.token}")
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(run(_parser().parse_args()))


if __name__ == "__main__":
    main()
