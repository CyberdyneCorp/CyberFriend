"""SQL for the corpus, and the one place the permission predicate is applied.

Every content-returning statement takes the viewer's channel set and binds it
into the WHERE clause. Nothing here can produce an unfiltered read, because
nothing here builds a query without that bind parameter.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.sql.elements import TextClause

# pgvector's iterative scan defaults to `off`. With a selective filter and
# iterative scan off, an approximate scan returns its k best rows *before*
# the filter is applied, so a restricted viewer receives fewer results than
# they asked for -- silently, and worst for the people in the fewest channels.
SESSION_SETUP = ("SET hnsw.iterative_scan = relaxed_order",)

UPSERT_MESSAGE = text("""
INSERT INTO message (
    id, channel_id, author_person_id, content, created_at, edited_at,
    reply_to_id, thread_id, search_tsv
) VALUES (
    :id, :channel_id, :author_person_id, :content, :created_at, :edited_at,
    :reply_to_id, :thread_id, to_tsvector('english', :content)
)
ON CONFLICT (id) DO UPDATE SET
    content = EXCLUDED.content,
    edited_at = EXCLUDED.edited_at,
    search_tsv = EXCLUDED.search_tsv,
    -- An edit un-deletes nothing, but a re-ingest of a live message must not
    -- resurrect a tombstone either.
    deleted_at = message.deleted_at
WHERE message.edited_at IS DISTINCT FROM EXCLUDED.edited_at
   OR message.content IS DISTINCT FROM EXCLUDED.content
""")

TOMBSTONE_MESSAGE = text("""
UPDATE message SET deleted_at = :at WHERE id = :id AND deleted_at IS NULL
""")

TOMBSTONE_WINDOWS_FOR_MESSAGE = text("""
UPDATE window SET deleted_at = :at
WHERE id IN (SELECT window_id FROM window_message WHERE message_id = :id)
  AND deleted_at IS NULL
""")

PURGE_CHANNEL = text("""
WITH w AS (DELETE FROM window WHERE channel_id = :channel_id RETURNING id)
DELETE FROM message WHERE channel_id = :channel_id
""")

# --- retrieval ---------------------------------------------------------
#
# `:channel_ids` is the viewer's readable set. It is bound into the same
# statement as the ranking so the index scan is constrained rather than its
# output filtered.

LEXICAL_SEARCH = text("""
SELECT w.id, w.channel_id, w.text, w.starts_at, w.ends_at,
       ts_rank_cd(w.search_tsv, plainto_tsquery('english', :q)) AS score
FROM window w
WHERE w.deleted_at IS NULL
  AND w.channel_id = ANY(:channel_ids)
  AND w.search_tsv @@ plainto_tsquery('english', :q)
  AND (:since IS NULL OR w.ends_at >= :since)
  AND (:until IS NULL OR w.starts_at <= :until)
ORDER BY score DESC
LIMIT :limit
""")

VECTOR_SEARCH = text("""
SELECT w.id, w.channel_id, w.text, w.starts_at, w.ends_at,
       1 - (w.embedding <=> :embedding) AS score
FROM window w
WHERE w.deleted_at IS NULL
  AND w.embedding IS NOT NULL
  AND w.channel_id = ANY(:channel_ids)
  AND (:since IS NULL OR w.ends_at >= :since)
  AND (:until IS NULL OR w.starts_at <= :until)
ORDER BY w.embedding <=> :embedding
LIMIT :limit
""")

WINDOW_MESSAGE_IDS = text("""
SELECT window_id, message_id FROM window_message
WHERE window_id = ANY(:window_ids) ORDER BY window_id, position
""")

THREAD_CONTEXT = text("""
WITH anchor AS (
    SELECT id, channel_id, thread_id, created_at
    FROM message
    WHERE id = :message_id AND deleted_at IS NULL
      AND channel_id = ANY(:channel_ids)
)
SELECT m.id, m.channel_id, m.author_person_id, m.content, m.created_at,
       m.edited_at, m.reply_to_id, m.thread_id
FROM message m, anchor a
WHERE m.deleted_at IS NULL
  AND m.channel_id = a.channel_id
  AND m.channel_id = ANY(:channel_ids)
  AND (a.thread_id IS NULL OR m.thread_id = a.thread_id)
  AND m.created_at BETWEEN a.created_at - INTERVAL '1 hour'
                       AND a.created_at + INTERVAL '1 hour'
ORDER BY m.created_at
LIMIT :limit
""")

LIST_CHANNELS = text("""
SELECT id FROM channel
WHERE is_indexed = TRUE AND id = ANY(:channel_ids)
ORDER BY name
""")


def statements() -> Sequence[TextClause]:
    """Every content-returning statement, for the test that audits them."""
    return (LEXICAL_SEARCH, VECTOR_SEARCH, THREAD_CONTEXT, LIST_CHANNELS)
