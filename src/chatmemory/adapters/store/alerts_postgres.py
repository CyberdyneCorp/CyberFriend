"""Implements `AlertStore`. The statements live next door in `alerts_sql`."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from chatmemory.adapters.store import alerts_sql
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.alerts import (
    DEFAULT_ALERTS_PER_PERSON,
    AddressSource,
    AlertKind,
    AlertLanguage,
    AlertRefusal,
    AlertState,
    AlertStore,
    AlertUpdate,
    LpProtocol,
    LpTarget,
    NewAlert,
    PositionAlert,
)

log = structlog.get_logger()

PLATFORM = "discord"


def _lp(row: RowMapping) -> LpTarget | None:
    if row["protocol"] is None:
        return None
    return LpTarget(
        protocol=LpProtocol(row["protocol"]),
        token_id=int(row["token_id"]),
        pool_ref=row["pool_ref"],
        token0_symbol=row["token0_symbol"],
        token1_symbol=row["token1_symbol"],
        token0_decimals=int(row["token0_decimals"]),
        token1_decimals=int(row["token1_decimals"]),
        fee=int(row["fee"]),
    )


def _alert(row: RowMapping, person: PersonRef) -> PositionAlert:
    pending = row["pending_state"]
    return PositionAlert(
        id=row["id"],
        person=person,
        kind=AlertKind(row["kind"]),
        chain=row["chain"],
        address=row["address"],
        address_source=AddressSource(row["address_source"]),
        language=AlertLanguage(row["language"]),
        state=AlertState(row["state"]),
        state_since=row["state_since"],
        created_at=row["created_at"],
        next_check_at=row["next_check_at"],
        lp=_lp(row),
        threshold=row["threshold"],
        notify_return=bool(row["notify_return"]),
        pending_state=AlertState(pending) if pending else None,
        pending_count=int(row["pending_count"]),
        last_value=row["last_value"],
        consecutive_failures=int(row["consecutive_failures"]),
        last_checked_at=row["last_checked_at"],
        last_fired_at=row["last_fired_at"],
        disabled_at=row["disabled_at"],
        disabled_reason=row["disabled_reason"] or "",
    )


def _insert_params(alert: NewAlert) -> dict[str, Any]:
    lp = alert.lp
    return {
        "kind": str(alert.kind),
        "chain": alert.chain,
        "address": alert.address.lower(),
        "address_source": str(alert.address_source),
        "protocol": str(lp.protocol) if lp else None,
        "token_id": Decimal(lp.token_id) if lp else None,
        "pool_ref": lp.pool_ref.lower() if lp else None,
        "token0_symbol": lp.token0_symbol if lp else None,
        "token1_symbol": lp.token1_symbol if lp else None,
        "token0_decimals": lp.token0_decimals if lp else None,
        "token1_decimals": lp.token1_decimals if lp else None,
        "fee": lp.fee if lp else None,
        "threshold": alert.threshold,
        "notify_return": alert.notify_return,
        "language": str(alert.language),
        "state": str(alert.state),
        "last_value": alert.last_value,
    }


class PostgresAlertStore:
    """Implements `AlertStore`."""

    def __init__(
        self,
        engine: AsyncEngine,
        cap: int = DEFAULT_ALERTS_PER_PERSON,
        platform: str = PLATFORM,
    ) -> None:
        self._engine = engine
        # Enforced inside the insert, as for scheduled tasks.
        self._cap = cap
        self._platform = platform

    async def _person_id(self, conn: AsyncConnection, person: PersonRef) -> int | None:
        rows = await conn.execute(
            alerts_sql.RESOLVE_PERSON_ID,
            {"platform": self._platform, "platform_user_id": person.platform_user_id},
        )
        found = rows.first()
        return None if found is None else int(found[0])

    async def create(
        self, alert: NewAlert, first_check_at: datetime
    ) -> PositionAlert | AlertRefusal:
        async with self._engine.begin() as conn:
            person_id = await self._person_id(conn, alert.person)
            if person_id is None:
                # Not a path that may invent identities; see schedules.
                return AlertRefusal.UNKNOWN_PERSON
            rows = await conn.execute(
                alerts_sql.CREATE_WITHIN_CAP,
                {
                    **_insert_params(alert),
                    "person_id": person_id,
                    "next_check_at": first_check_at,
                    "cap": self._cap,
                },
            )
            row = rows.mappings().first()
            if row is not None:
                return _alert(row, alert.person)
            active = await conn.scalar(alerts_sql.ACTIVE_COUNT, {"person_id": person_id})
            return AlertRefusal.AT_CAP if int(active or 0) >= self._cap else AlertRefusal.DUPLICATE

    async def for_person(self, person: PersonRef) -> Sequence[PositionAlert]:
        async with self._engine.connect() as conn:
            person_id = await self._person_id(conn, person)
            if person_id is None:
                return []
            rows = await conn.execute(alerts_sql.FOR_PERSON, {"person_id": person_id})
            return [_alert(r, person) for r in rows.mappings()]

    async def delete(self, person: PersonRef, alert_id: int) -> bool:
        async with self._engine.begin() as conn:
            person_id = await self._person_id(conn, person)
            if person_id is None:
                return False
            deleted = await conn.execute(
                alerts_sql.DELETE_OWN, {"alert_id": alert_id, "person_id": person_id}
            )
            return bool(deleted.rowcount)

    async def claim_due(
        self, now: datetime, limit: int, interval_seconds: float
    ) -> Sequence[PositionAlert]:
        async with self._engine.begin() as conn:
            rows = await conn.execute(
                alerts_sql.CLAIM_DUE,
                {
                    "now": now,
                    "limit": limit,
                    "interval": float(interval_seconds),
                    "platform": self._platform,
                },
            )
            claimed = []
            for row in rows.mappings():
                platform_user_id = row["platform_user_id"]
                if platform_user_id is None:
                    # Nobody to message, so nothing worth reading.
                    log.warning("alerts.owner_unresolvable", alert_id=row["id"])
                    continue
                owner = PersonRef(platform=self._platform, platform_user_id=int(platform_user_id))
                claimed.append(_alert(row, owner))
            return claimed

    async def record(self, alert_id: int, update: AlertUpdate, now: datetime) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                alerts_sql.RECORD,
                {
                    "alert_id": alert_id,
                    "state": str(update.state),
                    "state_since": update.state_since,
                    "pending_state": str(update.pending_state) if update.pending_state else None,
                    "pending_count": update.pending_count,
                    "last_value": update.last_value,
                    "failed": update.failed,
                    "fired": update.fired,
                    "disable_reason": update.disable_reason,
                    "now": now,
                },
            )

    async def disable(self, person: PersonRef, reason: str, now: datetime) -> int:
        async with self._engine.begin() as conn:
            person_id = await self._person_id(conn, person)
            if person_id is None:
                return 0
            stopped = await conn.execute(
                alerts_sql.DISABLE_FOR_PERSON,
                {"person_id": person_id, "reason": reason, "now": now},
            )
            return int(stopped.rowcount or 0)


if TYPE_CHECKING:  # pragma: no cover - exists to fail type-checking, not to run

    def _conforms(store: PostgresAlertStore) -> AlertStore:
        return store
