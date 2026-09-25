"""SQL for extracted decisions.

Writes only, for now. The read path, when it comes, keeps the rule `asks_sql.py`
keeps: the viewer's channels bound into the statement's own WHERE clause, and
the source and every evidence message required to be alive.
"""

from __future__ import annotations

from sqlalchemy import text

RESOLVE_PERSON_ID = text("""
SELECT person_id FROM person_platform_id
WHERE platform = :platform AND platform_user_id = :platform_user_id
""")

# An embedding that failed is written as NULL and kept NULL by a later run
# that also failed, but a later run that succeeded fills it in.
UPSERT_DECISION = text("""
INSERT INTO decision (
    decision_key, source_message_id, channel_id, thread_id, author_person_id,
    evidence_message_ids, summary, topic, confidence, decided_at, embedding
) VALUES (
    :decision_key, :source_message_id, :channel_id, CAST(:thread_id AS bigint),
    :author_person_id, CAST(:evidence_message_ids AS bigint[]), :summary, :topic,
    :confidence, :decided_at, CAST(:embedding AS vector)
)
ON CONFLICT (decision_key) DO UPDATE SET
    channel_id = EXCLUDED.channel_id,
    thread_id = EXCLUDED.thread_id,
    evidence_message_ids = EXCLUDED.evidence_message_ids,
    summary = EXCLUDED.summary,
    topic = EXCLUDED.topic,
    confidence = EXCLUDED.confidence,
    decided_at = EXCLUDED.decided_at,
    embedding = COALESCE(EXCLUDED.embedding, decision.embedding)
""")

# Withdraws what a re-run no longer finds. Unlike asks there is nothing to
# exempt: nobody can correct a decision yet, so extraction is the only word.
PRUNE_DECISIONS = text("""
DELETE FROM decision
WHERE source_message_id = :source_message_id
  AND decision_key <> ALL(CAST(:keep AS text[]))
""")

WITHDRAW_DECISIONS = text("""
DELETE FROM decision WHERE source_message_id = ANY(CAST(:source_message_ids AS bigint[]))
""")
