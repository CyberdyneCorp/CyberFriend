"""The voice-question ledger, over Postgres.

The opt-out check, both caps and the charge run in one transaction under one
advisory lock, so what is checked is what is charged.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from chatmemory.adapters.store import media_sql
from chatmemory.app.voice import Reservation, VoiceLedger, VoiceLimits
from chatmemory.domain.identity import PersonRef


class PostgresVoiceLedger:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def reserve(
        self, person: PersonRef, seconds: int, month: date, limits: VoiceLimits
    ) -> Reservation:
        async with self._engine.begin() as conn:
            await conn.execute(media_sql.LOCK_LEDGER)
            person_id, opted_out = await self._person(conn, person)
            if opted_out:
                return Reservation.OPTED_OUT
            usage = (
                await conn.execute(
                    media_sql.MONTH_USAGE, {"person_id": person_id, "month": month}
                )
            ).one()
            if usage.mine + seconds > limits.person_monthly_seconds:
                return Reservation.PERSON_CAP
            if usage.everyone + seconds > limits.overall_monthly_seconds:
                return Reservation.MONTHLY_CAP
            await conn.execute(
                media_sql.CHARGE, {"person_id": person_id, "month": month, "seconds": seconds}
            )
        return Reservation.GRANTED

    async def _person(self, conn: AsyncConnection, person: PersonRef) -> tuple[int, bool]:
        """The canonical person id and whether they opted out, created if unseen.

        Somebody who has never spoken in an indexed channel can still DM the
        bot, and their minutes need a row to be charged to.
        """
        ids = {"platform": person.platform, "platform_user_id": person.platform_user_id}
        found = (await conn.execute(media_sql.PERSON_OF, ids)).first()
        if found is not None:
            return int(found.person_id), bool(found.opted_out)
        created = await conn.execute(
            media_sql.CREATE_PERSON, {"display_name": str(person.platform_user_id)}
        )
        person_id = int(created.scalar_one())
        await conn.execute(media_sql.LINK_PERSON, {**ids, "person_id": person_id})
        return person_id, False


if TYPE_CHECKING:  # pragma: no cover - exists to fail type-checking, not to run

    def _conforms(ledger: PostgresVoiceLedger) -> VoiceLedger:
        return ledger
