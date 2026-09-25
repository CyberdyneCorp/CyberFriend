"""SQL for extracted decisions, and the one place their read predicate lives.

The read keeps the rule `asks_sql.py` keeps: the viewer's channels bound into
the statement's own WHERE clause, so the filter constrains the scan rather
than its output, and the source and every evidence message required to be
alive. A decision's summary may restate any message the model was shown, so
one deleted or unreadable evidence message is enough to withhold it.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.sql.elements import TextClause

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

# A deletion reaches every decision that rests on the message, not only the
# ones it stated: the evidence is what the summary may quote. The source is
# always in its own evidence, so one containment test covers both, and it is
# the form the GIN index on the array can answer.
WITHDRAW_MESSAGE_DECISIONS = text("""
DELETE FROM decision
WHERE evidence_message_ids @> ARRAY[CAST(:message_id AS bigint)]
""")


# --- reads ---------------------------------------------------------------
#
# Ranked by an exact scan, not an index: decisions are few, and an approximate
# nearest-neighbour index would be probed before the channel filter, returning
# neighbours the viewer may not read and missing ones they may. The score is
# 0.7 * cosine + 0.3 * ts_rank over the 'simple' tsvector (English stemming
# mangles Portuguese). With a topic, a row is kept only if its cosine clears
# the floor, or -- for a row stored without a vector -- if every meaningful
# word of the topic occurs in it. The best `:limit` are then listed newest
# first, because a later decision may replace an earlier one and the reader
# needs to see the order.

SEARCH_DECISIONS = text("""
WITH scored AS (
    SELECT d.source_message_id, d.channel_id, d.summary, d.topic, d.decided_at,
           p.display_name AS author_display,
           m.content AS source_content,
           COALESCE(1 - (d.embedding <=> CAST(:embedding AS vector)), 0) AS similarity,
           d.embedding IS NULL AS unembedded,
           d.search_tsv @@ plainto_tsquery('simple', :terms) AS lexical,
           0.7 * COALESCE(1 - (d.embedding <=> CAST(:embedding AS vector)), 0)
             + 0.3 * ts_rank(d.search_tsv, plainto_tsquery('simple', :terms)) AS score
    FROM decision d
    JOIN message m ON m.id = d.source_message_id AND m.deleted_at IS NULL
    JOIN person p ON p.id = d.author_person_id
    WHERE d.channel_id = ANY(:channel_ids)
      AND d.confidence >= :min_confidence
      AND (CAST(:since AS timestamptz) IS NULL OR d.decided_at >= CAST(:since AS timestamptz))
      AND (CAST(:until AS timestamptz) IS NULL OR d.decided_at < CAST(:until AS timestamptz))
      -- Every evidence message still exists, is alive, and is readable.
      AND NOT EXISTS (
          SELECT 1
          FROM unnest(d.evidence_message_ids) AS e(message_id)
          LEFT JOIN message em ON em.id = e.message_id
          WHERE em.id IS NULL
             OR em.deleted_at IS NOT NULL
             OR NOT em.channel_id = ANY(:channel_ids)
      )
), best AS (
    SELECT * FROM scored
    WHERE NOT CAST(:has_topic AS boolean)
       OR similarity >= :min_similarity
       OR (lexical AND unembedded)
    ORDER BY score DESC, decided_at DESC
    LIMIT :limit
)
SELECT source_message_id, channel_id, summary, topic, decided_at,
       author_display, source_content
FROM best
ORDER BY decided_at DESC, source_message_id DESC
""")


def statements() -> Sequence[TextClause]:
    """Every statement that reads decisions, for the test that audits them."""
    return (SEARCH_DECISIONS,)
