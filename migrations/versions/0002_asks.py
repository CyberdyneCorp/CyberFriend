"""Extracted asks, their corrections, and the reactions that close them.

Revision ID: 0002
Revises: 0001
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

ADDRESSEE_KINDS = ("person", "group", "unattributed")
ASK_KINDS = ("request", "question", "commitment")
ASK_STATUSES = ("open", "answered", "stale")
CORRECTION_RESOLUTIONS = ("done", "not_applicable")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.create_table(
        "ask",
        sa.Column("id", sa.BigInteger, primary_key=True),
        # Stable identity derived from the source message, the kind of ask and
        # its addressee. Re-extraction upserts on this key, so a model that
        # phrases the same obligation differently on a second pass updates the
        # row rather than creating a duplicate obligation.
        sa.Column("ask_key", sa.Text, nullable=False, unique=True),
        sa.Column(
            "source_message_id",
            sa.BigInteger,
            sa.ForeignKey("message.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Denormalised for the same reason as on conversation_window: the
        # viewer's readable set has to constrain the scan that ranks asks, not
        # filter its output. An ask is a *summary* of a private conversation,
        # so a post-filter here leaks more than it would for raw text.
        sa.Column("channel_id", sa.BigInteger, sa.ForeignKey("channel.id"), nullable=False),
        sa.Column("thread_id", sa.BigInteger),
        sa.Column(
            "requester_person_id", sa.BigInteger, sa.ForeignKey("person.id"), nullable=False
        ),
        # Nullable on purpose: "we could not tell who this fell to" is a value
        # we record, never a person we pick.
        sa.Column("addressee_kind", sa.Text, nullable=False),
        sa.Column("addressee_person_id", sa.BigInteger, sa.ForeignKey("person.id")),
        sa.Column("addressee_group", sa.Text),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="open"),
        sa.Column("asked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        # Which observable event closed it: 'reply' or 'reaction'. Never a
        # model's opinion, which is why this is an event name and not a score.
        sa.Column("closed_by", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            _in_list("addressee_kind", ADDRESSEE_KINDS), name="ck_ask_addressee_kind"
        ),
        sa.CheckConstraint(_in_list("kind", ASK_KINDS), name="ck_ask_kind"),
        sa.CheckConstraint(_in_list("status", ASK_STATUSES), name="ck_ask_status"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_ask_confidence"),
        # The invariant behind "never guess": a person id may only be present
        # when the addressee kind actually names a person.
        sa.CheckConstraint(
            "(addressee_kind = 'person') = (addressee_person_id IS NOT NULL)",
            name="ck_ask_person_addressee",
        ),
        sa.CheckConstraint(
            "(addressee_kind = 'group') = (addressee_group IS NOT NULL)",
            name="ck_ask_group_addressee",
        ),
    )
    # The shape every obligation question uses: this person, most recent first.
    op.create_index("ix_ask_addressee_time", "ask", ["addressee_person_id", "asked_at"])
    # Commitments are answered by requester, since the speaker is the one who
    # owes the thing they promised.
    op.create_index("ix_ask_requester_time", "ask", ["requester_person_id", "asked_at"])
    op.create_index("ix_ask_channel_time", "ask", ["channel_id", "asked_at"])
    op.create_index("ix_ask_source_message", "ask", ["source_message_id"])

    op.create_table(
        "ask_correction",
        # Keyed by ask_key rather than by the row id, so a correction is bound
        # to the *obligation* rather than to one extraction of it. Re-running
        # extraction over the source rewrites the ask row and leaves this
        # untouched, which is what "corrections outrank extraction" means.
        sa.Column(
            "ask_key",
            sa.Text,
            sa.ForeignKey("ask.ask_key", ondelete="CASCADE"),
            primary_key=True,
        ),
        # Only the addressee may correct their own asks; enforced in the
        # statement that writes this, and recorded here so the record says who.
        sa.Column("by_person_id", sa.BigInteger, sa.ForeignKey("person.id"), nullable=False),
        sa.Column("resolution", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            _in_list("resolution", CORRECTION_RESOLUTIONS), name="ck_correction_resolution"
        ),
    )

    # Reactions are observed events, kept only for the messages asks came from.
    # They exist so state transitions can be decided from what happened rather
    # than from a model's reading of the conversation.
    op.create_table(
        "ask_reaction",
        sa.Column(
            "message_id",
            sa.BigInteger,
            sa.ForeignKey("message.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("person_id", sa.BigInteger, sa.ForeignKey("person.id"), nullable=False),
        sa.Column("emoji", sa.Text, nullable=False),
        sa.Column("reacted_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("message_id", "person_id", "emoji"),
    )


def downgrade() -> None:
    for table in ("ask_reaction", "ask_correction", "ask"):
        op.drop_table(table)
