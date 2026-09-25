"""One delete path for everything derived from a person: `purge_person_derived`.

Opt-out used to purge through four triggers on `person_opt_out`, one per table
(0013 memory, 0014 facts, 0015 notifications, 0020 alerts), and every table
added since had to remember to add a fifth. Two did not: a scheduled task kept
running for somebody who had opted out and kept messaging them, and an MCP
token kept authenticating as them.

This folds those bodies into one SQL function and adds `scheduled_task` and
`mcp_token`. The `person_opt_out` trigger calls it; self-service erasure calls
it directly. A later table holding person data adds its DELETE here in its own
migration, so opt-out and erasure can never drift apart.

Every statement is a plain DELETE keyed on the person, so calling it again is
a no-op -- which is what lets a resumed erasure repeat the step safely.

Revision ID: 0028
Revises: 0027
"""
from __future__ import annotations

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

#: The statements the function runs, in order, over its `p_person_id` argument.
#:
#: `mcp_token` is keyed on the platform identity, not the person row, so it is
#: reached through `person_platform_id`. The row is deleted rather than marked
#: revoked: a revoked row keeps who held a credential, and erasure promises no
#: `mcp_token` rows remain for the person.
PURGES = (
    "DELETE FROM conversation_turn WHERE person_id = p_person_id",
    "DELETE FROM conversation_summary WHERE person_id = p_person_id",
    "DELETE FROM person_fact WHERE person_id = p_person_id",
    "DELETE FROM notification WHERE person_id = p_person_id",
    "DELETE FROM position_alert WHERE person_id = p_person_id",
    "DELETE FROM scheduled_task WHERE person_id = p_person_id",
    """DELETE FROM mcp_token t
        USING person_platform_id p
        WHERE p.person_id = p_person_id
          AND t.platform = p.platform
          AND t.platform_user_id = p.platform_user_id""",
)

PURGE_FUNCTION = (
    "CREATE OR REPLACE FUNCTION purge_person_derived(p_person_id bigint) "
    "RETURNS void AS $$\nBEGIN\n"
    + "".join(f"    {statement};\n" for statement in PURGES)
    + "END;\n$$ LANGUAGE plpgsql"
)

ON_OPT_OUT = """
CREATE OR REPLACE FUNCTION purge_person_derived_on_opt_out() RETURNS trigger AS $$
BEGIN
    PERFORM purge_person_derived(NEW.person_id);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql
"""

TRIGGER = "trg_person_opt_out_purges_derived"

#: The triggers this replaces: (trigger, function, tables it purged).
REPLACED = (
    ("trg_person_opt_out_purges_memory", "purge_memory_on_opt_out",
     ("conversation_turn", "conversation_summary")),
    ("trg_person_opt_out_purges_facts", "purge_facts_on_opt_out", ("person_fact",)),
    ("trg_person_opt_out_purges_notifications", "purge_notifications_on_opt_out",
     ("notification",)),
    ("trg_person_opt_out_purges_position_alerts", "purge_position_alerts_on_opt_out",
     ("position_alert",)),
)


def upgrade() -> None:
    for trigger, function, _ in REPLACED:
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON person_opt_out")
        op.execute(f"DROP FUNCTION IF EXISTS {function}()")
    op.execute(PURGE_FUNCTION)
    op.execute(ON_OPT_OUT)
    op.execute(
        f"CREATE TRIGGER {TRIGGER} "
        "AFTER INSERT OR UPDATE ON person_opt_out "
        "FOR EACH ROW EXECUTE FUNCTION purge_person_derived_on_opt_out()"
    )
    # People who opted out before this revision still have the rows the old
    # triggers missed. Purging them now is what makes the fix reach them.
    op.execute("SELECT purge_person_derived(person_id) FROM person_opt_out")


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {TRIGGER} ON person_opt_out")
    op.execute("DROP FUNCTION IF EXISTS purge_person_derived_on_opt_out()")
    op.execute("DROP FUNCTION IF EXISTS purge_person_derived(bigint)")
    for trigger, function, tables in REPLACED:
        deletes = "".join(
            f"    DELETE FROM {table} WHERE person_id = NEW.person_id;\n" for table in tables
        )
        op.execute(
            f"CREATE OR REPLACE FUNCTION {function}() RETURNS trigger AS $$\nBEGIN\n"
            f"{deletes}    RETURN NULL;\nEND;\n$$ LANGUAGE plpgsql"
        )
        op.execute(
            f"CREATE TRIGGER {trigger} AFTER INSERT OR UPDATE ON person_opt_out "
            f"FOR EACH ROW EXECUTE FUNCTION {function}()"
        )
