"""`/privacy`: what each view shows, what it states, and where it reads.

The channel view is rendered from `Inventory.summary()`, so no value of any
kind can reach it; the DM view shows values. The retention statements come
from settings. The service reads channel content only for the channels the
`/channels` listing gives.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from chatmemory import composition
from chatmemory.adapters.discord.privacy import (
    MESSAGE_EMBED_CHARS,
    MESSAGE_EMBEDS,
    Section,
    channel_sections,
    direct_sections,
    kept_section,
    pages,
)
from chatmemory.app.channel_listing import ChannelListingService
from chatmemory.app.facts import DIRECT_ONLY_KINDS
from chatmemory.app.language import Language
from chatmemory.app.privacy import PrivacyService, RetentionFacts
from chatmemory.composition import build_privacy
from chatmemory.config import Settings
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.ports.facts import FactKind
from chatmemory.ports.privacy import (
    ArchivedChannel,
    HeldAlert,
    HeldFact,
    HeldSuggestion,
    HeldTask,
    HeldToken,
    Inventory,
    MediaCounts,
    MemoryCounts,
    NotificationSetting,
)

EN, PT = Language.ENGLISH, Language.PORTUGUESE
PERSON = PersonRef("discord", 42)
WALLET = "0xb26b933a075fbb3d4e8b0925cad4f2bc345475e0"

VALUES = {
    FactKind.EMAIL: "leo@example.com",
    FactKind.PHONE: "+5521980703795",
    FactKind.HOME_ADDRESS: "Rua das Flores 12",
    FactKind.BIRTH_DATE: "1981-06-21",
    FactKind.FULL_NAME: "Leonardo Araujo",
    FactKind.ETH_WALLET: WALLET,
    FactKind.PREFERRED_NAME: "Leo",
}

INVENTORY = Inventory(
    known=True,
    platforms=("discord",),
    facts=tuple(HeldFact(kind, value) for kind, value in VALUES.items()),
    memory=MemoryCounts(direct_turns=3, direct_summaries=1, channel_turns=2),
    recent_questions=("when is the secret launch?",),
    tasks=(HeldTask(7, "any news on the treasury report?", 24, "nothing", False),),
    alerts=(HeldAlert(3, "lp_range", "base", "0xabcdef0000000000000000000000000000001234", None),),
    notifications=NotificationSetting(enabled=False, queued=2),
    voice_seconds_this_month=90,
    media=MediaCounts(by_kind=(("image", 2), ("voice", 1)), with_text=1),
    suggestions=(HeldSuggestion(5, "weekly digest of decisions", "planned"),),
    tokens=(HeldToken("laptop", datetime(2026, 8, 1, tzinfo=UTC)),),
    archived=(ArchivedChannel(ChannelRef("discord", 100), 4),),
    traces=6,
)

RETENTION = RetentionFacts(
    tracing=True, trace_retention_days=90, memory_retention_days=30, backup_retention_days=None
)


def _text(sections: Sequence[Section], language: Language = EN) -> str:
    return "\n".join(f"{s.title}\n{s.description(language)}" for s in sections)


# --- the channel view ---------------------------------------------------------------


def test_no_fact_value_reaches_the_channel_view() -> None:
    shown = _text(channel_sections(INVENTORY.summary(), RETENTION, EN))

    for kind, value in VALUES.items():
        assert value not in shown, f"{kind} value in a server channel"
    assert WALLET[-4:] not in shown
    assert "email address" in shown and "home address" in shown


@pytest.mark.parametrize("kind", sorted(DIRECT_ONLY_KINDS))
def test_direct_only_kinds_are_named_but_never_valued_in_a_channel(kind: FactKind) -> None:
    inventory = Inventory(known=True, facts=(HeldFact(kind, VALUES.get(kind, "x@y.z")),))

    shown = _text(channel_sections(inventory.summary(), RETENTION, EN))

    assert VALUES.get(kind, "x@y.z") not in shown


def test_the_channel_view_counts_but_never_quotes() -> None:
    shown = _text(channel_sections(INVENTORY.summary(), RETENTION, EN))

    for private in (
        "secret launch",
        "treasury report",
        "weekly digest",
        "laptop",
        "1234",
    ):
        assert private not in shown
    assert "3 questions and answers in direct messages, 2 in server channels" in shown
    assert "<#100>: 4 messages" in shown
    assert "3 on your archived messages" in shown


def test_the_summary_type_has_nowhere_to_put_a_value() -> None:
    summary = INVENTORY.summary()

    assert summary.fact_kinds == tuple(VALUES)
    assert not any(hasattr(summary, name) for name in ("facts", "recent_questions"))


# --- the DM view --------------------------------------------------------------------


def test_the_dm_view_shows_the_values() -> None:
    shown = _text(direct_sections(INVENTORY, RETENTION, EN))

    assert "`leo@example.com`" in shown and "+5521980703795" in shown
    assert "21 June 1981" in shown
    assert "when is the secret launch?" in shown
    assert "**7** - every 24h - any news on the treasury report? (found nothing)" in shown
    assert "**3** - LP range - base `…1234`" in shown
    assert "**#5** - planned - weekly digest of decisions" in shown
    assert "laptop - created 2026-08-01" in shown
    assert "3 on your archived messages" in shown
    assert "2 images, 1 voice notes; 1 of them transcribed or described." in shown
    assert "Direct messages about things asked of you: off." in shown
    assert "Waiting to be sent: 2." in shown
    assert "1.5 minutes of your audio transcribed this month." in shown


def test_an_empty_inventory_says_none_rather_than_nothing() -> None:
    shown = _text(direct_sections(Inventory(), RETENTION, EN))

    assert "Personal details\nNone." in shown
    assert "on (you never changed it)" in shown
    assert "No messages of yours are archived in channels you can read." in shown


def test_an_opted_out_person_is_told_nothing_is_archived() -> None:
    shown = _text(channel_sections(Inventory(known=True, archiving=False).summary(), RETENTION, EN))

    assert "You've opted out: I don't archive, remember or trace anything you send." in shown


# --- the statements ---------------------------------------------------------------


def test_trace_retention_and_admin_access_are_stated_from_settings() -> None:
    facts = RetentionFacts(True, 45, 30, None)

    shown = _text(channel_sections(INVENTORY.summary(), facts, EN))

    assert "recorded for up to 45 days, and admins can read them" in shown
    assert "Recorded now: at least 6 of your questions." in shown
    assert "90 days" not in shown


def test_without_tracing_nothing_is_claimed_recorded() -> None:
    facts = RetentionFacts(False, 90, 30, None)

    shown = _text(channel_sections(Inventory(known=True).summary(), facts, EN))

    assert "I don't record your questions and answers in this deployment." in shown
    assert "Recorded now" not in shown and "admins can read" not in shown


def test_no_backup_retention_setting_says_no_backups_are_kept() -> None:
    shown = _text([kept_section(RETENTION, EN)])

    assert "No database backups are kept" in shown
    assert "age out" not in shown


def test_a_backup_retention_setting_is_the_period_stated() -> None:
    shown = _text([kept_section(RetentionFacts(True, 90, 30, 14), EN)])

    assert "Database backups, until they age out after 14 days." in shown


def test_the_kept_list_names_everything_that_survives_a_deletion() -> None:
    shown = _text([kept_section(RetentionFacts(True, 90, 21, 7), EN)])

    for survivor in (
        "A minimal record of you",
        "admin change log",
        "Database backups",
        "CyberdyneAuth account",
        "Other people's messages that mention you",
        "Other people's remembered answers",
        "expire within 21 days",
        "Messages I already sent on Discord",
        "An anonymous total of voice minutes",
    ):
        assert survivor in shown


def test_the_kept_list_describes_what_delete_everything_leaves() -> None:
    """The kept list is worded for erasure: the tombstone and the anonymous
    voice total, not the name and per-person minutes an opt-out keeps."""
    shown = _text([kept_section(RETENTION, EN)])

    assert "Your name and preferences are cleared" in shown
    assert "An anonymous total of voice minutes" in shown
    assert "still under your record" not in shown
    assert "your name and your notification setting" not in shown


def test_the_statements_are_in_portuguese_for_a_portuguese_caller() -> None:
    shown = _text(channel_sections(INVENTORY.summary(), RETENTION, PT), PT)

    assert "ficam registradas por até 90 dias, e os administradores podem lê-las" in shown
    assert "Não são mantidos backups do banco de dados" in shown
    assert "Mantido mesmo quando seus dados são apagados" in shown
    assert "Kept even" not in shown


# --- fitting Discord's limits ------------------------------------------------------


def test_a_long_section_is_cut_and_says_how_much_was_left_out() -> None:
    lines = tuple(f"**#{n}** - new - " + "x" * 90 for n in range(100))

    description = Section("Suggestions", lines).description(EN)

    assert len(description) <= 4096
    assert description.endswith("more.")
    assert "…and" in description


def test_pages_respect_the_embed_limits() -> None:
    sections = [Section(f"S{n}", ("y" * 1500,)) for n in range(14)]

    split = pages(sections, EN)

    assert sum(len(page) for page in split) == 14
    for page in split:
        assert len(page) <= MESSAGE_EMBEDS
        assert sum(len(embed) for embed in page) <= MESSAGE_EMBED_CHARS


def test_the_real_report_fits_one_message_per_page() -> None:
    for page in pages(direct_sections(INVENTORY, RETENTION, PT), PT):
        assert len(page) <= MESSAGE_EMBEDS
        assert sum(len(embed) for embed in page) <= 6000


# --- the service -----------------------------------------------------------------


class _Store:
    def __init__(self) -> None:
        self.calls: list[tuple[PersonRef, tuple[int, ...], date]] = []

    async def inventory(
        self, person: PersonRef, readable_channel_ids: Sequence[int], month: date
    ) -> Inventory:
        self.calls.append((person, tuple(readable_channel_ids), month))
        return Inventory(known=True)


class _Scope:
    def current(self) -> frozenset[int]:
        return frozenset({100, 300})


class _Acl:
    async def resolve_viewer(self, person: PersonRef) -> Viewer:
        return Viewer(person, frozenset({ChannelRef("discord", 100), ChannelRef("discord", 555)}))


def _now() -> datetime:
    return datetime(2026, 9, 25, 12, tzinfo=UTC)


async def test_only_archived_channels_the_person_can_read_are_read() -> None:
    store = _Store()
    listing = ChannelListingService(_Scope(), _Acl())
    service = PrivacyService(store, RETENTION, listing, clock=_now)

    report = await service.report(PERSON)

    assert store.calls == [(PERSON, (100,), date(2026, 9, 1))]
    assert report.retention is RETENTION


async def test_without_a_listing_no_channel_is_read() -> None:
    store = _Store()

    await PrivacyService(store, RETENTION, None, clock=_now).report(PERSON)

    assert store.calls[0][1] == ()


# --- the setting -------------------------------------------------------------------

BASE = {
    "discord_token": "t",
    "discord_guild_id": 1,
    "database_url": "postgresql+asyncpg://u:p@localhost/db",
    "llm_api_key": "k",
}


def test_backup_retention_is_unset_by_default_and_when_blank() -> None:
    assert Settings(**BASE).backup_retention_days is None  # type: ignore[arg-type]
    assert Settings(**BASE, backup_retention_days="").backup_retention_days is None  # type: ignore[arg-type]
    assert Settings(**BASE, backup_retention_days=14).backup_retention_days == 14  # type: ignore[arg-type]


def test_a_zero_backup_retention_is_refused() -> None:
    with pytest.raises(ValidationError):
        Settings(**BASE, backup_retention_days=0)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "tracing"),
    [
        ({"tracing_enabled": True, "langfuse_host": "https://lf.example"}, True),
        ({"tracing_enabled": True}, False),
        ({"tracing_enabled": False, "langfuse_host": "https://lf.example"}, False),
    ],
)
async def test_build_privacy_states_the_configured_retention(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, object], tracing: bool
) -> None:
    """The wiring passes every period from settings, and "tracing" only when
    `build_tracer` would export: enabled and a Langfuse host set."""
    monkeypatch.setattr(composition, "PostgresPrivacyStore", lambda engine: _Store())
    settings = Settings(
        **BASE,  # type: ignore[arg-type]
        trace_retention_days=45,
        memory_retention_days=7,
        backup_retention_days=14,
        **overrides,  # type: ignore[arg-type]
    )

    report = await build_privacy(settings, object(), None, clock=_now).report(  # type: ignore[arg-type]
        PERSON
    )

    assert report.retention == RetentionFacts(tracing, 45, 7, 14)
