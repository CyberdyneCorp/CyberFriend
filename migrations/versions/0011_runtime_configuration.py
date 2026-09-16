"""Settings an operator may change while the system runs.

Revision ID: 0011
Revises: 0012
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
# Chained *after* 0012 although it is numbered before it. 0012 landed first,
# pointing at 0010 and saying in writing that whoever lands 0011 re-chains it;
# this is that. Two heads are not a naming problem, they are an `alembic
# upgrade` that refuses to run, and a schema that cannot be migrated is a
# feature that never runs -- which is the failure this project keeps having.
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One table. `config_audit`, which records who changed each of these and
    # what it replaced, is created by 0012 together with the trigger that makes
    # it append-only; creating a second one here would be two tables with one
    # name, and the migration that ran second would fail.
    #
    # Values are text, whatever the setting's type. A form submits text, the
    # audit has to hold exactly what was written, and the parse that turns
    # "100 200" into a set of channel ids belongs where the setting's meaning
    # is known -- which is also the only place that can keep the previous value
    # when the text does not parse. A typed column moves that decision into the
    # database, where the only options are to accept a bad value or to fail the
    # write; a process that is answering questions must do neither.
    #
    # Secrets are absent by construction rather than by permission: nothing
    # resolves `discord_token`, `llm_api_key` or `database_url` from this
    # table, so a row inserted under one of those names is reported and
    # ignored. They are also needed *to reach* this database, which makes
    # storing them in it circular.
    op.create_table(
        "app_setting",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        # Who last wrote it. Not nullable: a stored setting exists because a
        # person made it exist, and "the token did it" is the attribution this
        # whole change was made to stop accepting.
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("app_setting")
