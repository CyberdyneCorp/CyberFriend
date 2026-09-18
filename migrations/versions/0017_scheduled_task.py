"""Questions a person asked to have asked on their behalf.

Revision ID: 0017
Revises: 0016
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

# What the last run did. Kept rather than inferred, because a person who is
# told nothing needs to be able to see *why* they are told nothing: "it ran
# and found nothing" and "it has not run since Tuesday" are the same silence
# from outside, and only one of them is working.
#
#   reported   an answer was produced and sent
#   nothing    the run abstained; nothing was sent, by design
#   failed     the run itself failed
#   closed     the person's direct messages could not be delivered to
OUTCOMES = ("reported", "nothing", "failed", "closed")

MIN_INTERVAL_HOURS = 1
MAX_INTERVAL_HOURS = 24


def upgrade() -> None:
    op.create_table(
        "scheduled_task",
        sa.Column("id", sa.BigInteger, primary_key=True),
        # Cascading is the whole of "removing a person removes their tasks".
        # Opt-out and erasure already purge facts, memory and notifications;
        # a scheduled task that outlived them would keep messaging somebody
        # who asked to be gone, on a timer, with nothing left to stop it.
        sa.Column(
            "person_id",
            sa.BigInteger,
            sa.ForeignKey("person.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The question, as the person typed it. Replayed verbatim on every
        # run: it is their own words, which is also what lets the egress
        # guard root an outbound query in it exactly as it would a live one.
        sa.Column("question", sa.Text, nullable=False),
        # Bounded here as well as in the command. A command is one way in;
        # the floor is a cost control and the ceiling is what makes "daily"
        # mean daily, and neither should depend on the only caller today
        # remembering to check.
        sa.Column("interval_hours", sa.Integer, nullable=False),
        sa.CheckConstraint(
            f"interval_hours >= {MIN_INTERVAL_HOURS} "
            f"AND interval_hours <= {MAX_INTERVAL_HOURS}",
            name="ck_scheduled_task_interval",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_outcome", sa.Text, nullable=True),
        sa.CheckConstraint(
            "last_outcome IS NULL OR last_outcome IN "
            + str(tuple(OUTCOMES)).replace('"', "'"),
            name="ck_scheduled_task_outcome",
        ),
        # Set when the person's direct messages are closed. A disabled task is
        # kept rather than deleted: "why did this stop?" is the question this
        # feature will actually be asked, and a deleted row answers it with
        # silence -- the same reasoning as the notification queue's outcomes.
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_reason", sa.Text, nullable=True),
    )
    # The sweep's only scan: due, and not disabled. Partial, so it stays the
    # size of the work outstanding rather than of every task ever created.
    op.create_index(
        "ix_scheduled_task_due",
        "scheduled_task",
        ["next_run_at"],
        postgresql_where=sa.text("disabled_at IS NULL"),
    )
    # Listing and the per-person cap both read by owner.
    op.create_index("ix_scheduled_task_person", "scheduled_task", ["person_id"])


def downgrade() -> None:
    op.drop_index("ix_scheduled_task_person", table_name="scheduled_task")
    op.drop_index("ix_scheduled_task_due", table_name="scheduled_task")
    op.drop_table("scheduled_task")
