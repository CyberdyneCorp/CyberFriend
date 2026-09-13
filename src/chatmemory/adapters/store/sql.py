"""SQL for the corpus, and the one place the permission predicate is applied.

Every content-returning statement takes the viewer's channel set and binds it
into the WHERE clause. Nothing here can produce an unfiltered read, because
nothing here builds a query without that bind parameter.

The one exception is the windowing and embedding maintenance section, which
runs for the ingestion process rather than for a person; it is marked as such
and is unreachable from any read path.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.sql.elements import TextClause


def vector_literal(values: Sequence[float]) -> str:
    """Render an embedding for a `vector` bind parameter.

    asyncpg has no native pgvector codec, so the value is passed as text and
    cast in the statement. Passing a Python list instead fails at execution
    with a type error that names the parameter, not the cause.
    """
    return "[" + ",".join(repr(float(v)) for v in values) + "]"

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
UPDATE conversation_window SET deleted_at = :at
WHERE id IN (SELECT window_id FROM conversation_window_message WHERE message_id = :id)
  AND deleted_at IS NULL
""")

PURGE_CHANNEL = text("""
WITH w AS (DELETE FROM conversation_window WHERE channel_id = :channel_id RETURNING id)
DELETE FROM message WHERE channel_id = :channel_id
""")

# --- windowing and embedding maintenance --------------------------------
#
# These run for the ingestion process, which acts for nobody: there is no
# viewer to bind, and rebuilding a window over only the channels some person
# may read would leave the rest of the corpus permanently unwindowed. They are
# deliberately absent from `statements()` for that reason -- none of them is
# reachable from a read path, and the audit there covers the statements that
# are. Every one still excludes tombstones, because withdrawn content must not
# re-enter a window or a vector either.

MESSAGES_WITHOUT_WINDOW = text("""
SELECT m.id, m.channel_id, m.content, m.created_at, m.edited_at,
       m.reply_to_id, m.thread_id,
       -- The corpus stores the canonical person; windowing renders the
       -- platform account, so it is resolved here. A person may carry more
       -- than one account on a platform, so this is a scalar subquery and
       -- not a join: a join would emit the message once per alias.
       COALESCE((
           SELECT p.platform_user_id FROM person_platform_id p
           WHERE p.person_id = m.author_person_id AND p.platform = :platform
           ORDER BY p.platform_user_id LIMIT 1
       ), m.author_person_id) AS platform_user_id
FROM message m
WHERE m.deleted_at IS NULL
  -- Not "has no membership row" but "is in no *live* window". Tombstoning a
  -- message withdraws the whole window containing it, and its surviving
  -- neighbours must come back through here to be re-formed -- otherwise one
  -- deletion silently takes its whole conversation out of retrieval.
  AND NOT EXISTS (
      SELECT 1 FROM conversation_window_message wm
      JOIN conversation_window w ON w.id = wm.window_id
      WHERE wm.message_id = m.id AND w.deleted_at IS NULL
  )
ORDER BY m.created_at, m.id
LIMIT :limit
""")

# Read before the delete below, so a rebuild that changes nothing keeps its
# vectors: re-embedding text that did not change is the dominant cost here.
SUPERSEDED_EMBEDDINGS = text("""
SELECT w.text, CAST(w.embedding AS text) AS embedding
FROM conversation_window w
WHERE w.channel_id = :channel_id
  AND w.embedding IS NOT NULL
  AND EXISTS (
      SELECT 1 FROM conversation_window_message wm
      WHERE wm.window_id = w.id AND wm.message_id = ANY(:message_ids)
  )
""")

# Scoped to the windows covering the messages being rebuilt, never to the
# whole channel: the caller re-windows a batch at a time, so clearing the
# channel would orphan every message outside the batch and put it straight
# back on the pending list -- a loop that re-embeds the channel forever.
DELETE_WINDOWS_FOR_MESSAGES = text("""
DELETE FROM conversation_window w
WHERE w.channel_id = :channel_id
  AND EXISTS (
      SELECT 1 FROM conversation_window_message wm
      WHERE wm.window_id = w.id AND wm.message_id = ANY(:message_ids)
  )
""")

INSERT_WINDOW = text("""
INSERT INTO conversation_window (
    channel_id, text, starts_at, ends_at, thread_id, embedding, search_tsv
) VALUES (
    :channel_id, :text, :starts_at, :ends_at, CAST(:thread_id AS bigint),
    CAST(:embedding AS vector), to_tsvector('english', :text)
)
RETURNING id
""")

# WITH ORDINALITY carries the window's message order, so membership costs one
# statement per window rather than one per message.
INSERT_WINDOW_MESSAGES = text("""
INSERT INTO conversation_window_message (window_id, message_id, position)
SELECT :window_id, m.id, m.ord - 1
FROM unnest(CAST(:message_ids AS bigint[])) WITH ORDINALITY AS m(id, ord)
ON CONFLICT (window_id, message_id) DO NOTHING
""")

# Newest first: recent conversation is what people ask about, so a backlog
# that is still draining should make the newest windows searchable first.
WINDOWS_MISSING_EMBEDDINGS = text("""
SELECT w.id, w.channel_id, w.text, w.starts_at, w.ends_at, w.thread_id,
       ARRAY(
           SELECT wm.message_id FROM conversation_window_message wm
           WHERE wm.window_id = w.id ORDER BY wm.position
       ) AS message_ids
FROM conversation_window w
WHERE w.deleted_at IS NULL AND w.embedding IS NULL
ORDER BY w.starts_at DESC, w.id DESC
LIMIT :limit
""")

STORE_EMBEDDING = text("""
UPDATE conversation_window SET embedding = CAST(:embedding AS vector)
WHERE id = :window_id AND deleted_at IS NULL
""")

SET_DISPLAY_NAME = text("""
UPDATE person SET display_name = :display_name
WHERE id = :person_id AND display_name IS DISTINCT FROM :display_name
""")

# --- retrieval ---------------------------------------------------------
#
# `:channel_ids` is the viewer's readable set. It is bound into the same
# statement as the ranking so the index scan is constrained rather than its
# output filtered.

LEXICAL_SEARCH = text("""
SELECT w.id, w.channel_id, w.text, w.starts_at, w.ends_at,
       ts_rank_cd(w.search_tsv, plainto_tsquery('english', :q)) AS score
FROM conversation_window w
WHERE w.deleted_at IS NULL
  AND w.channel_id = ANY(:channel_ids)
  AND w.search_tsv @@ plainto_tsquery('english', :q)
  AND (CAST(:since AS timestamptz) IS NULL OR w.ends_at >= CAST(:since AS timestamptz))
  AND (CAST(:until AS timestamptz) IS NULL OR w.starts_at <= CAST(:until AS timestamptz))
ORDER BY score DESC
LIMIT :limit
""")

VECTOR_SEARCH = text("""
SELECT w.id, w.channel_id, w.text, w.starts_at, w.ends_at,
       1 - (w.embedding <=> CAST(:embedding AS vector)) AS score
FROM conversation_window w
WHERE w.deleted_at IS NULL
  AND w.embedding IS NOT NULL
  AND w.channel_id = ANY(:channel_ids)
  AND (CAST(:since AS timestamptz) IS NULL OR w.ends_at >= CAST(:since AS timestamptz))
  AND (CAST(:until AS timestamptz) IS NULL OR w.starts_at <= CAST(:until AS timestamptz))
ORDER BY w.embedding <=> CAST(:embedding AS vector)
LIMIT :limit
""")

WINDOW_MESSAGE_IDS = text("""
SELECT window_id, message_id FROM conversation_window_message
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


# --- window rebuild scheduling -----------------------------------------
#
# A channel is marked dirty from the moment its content changed; the rebuild
# re-forms every window from there. Keyed on the EARLIEST pending change, so
# a later edit cannot move the watermark forward past work not yet done.

MARK_WINDOWS_DIRTY = text("""
INSERT INTO ingest_cursor (channel_id, windows_dirty_from)
VALUES (:channel_id, CAST(:at AS timestamptz))
ON CONFLICT (channel_id) DO UPDATE SET
    windows_dirty_from = LEAST(
        COALESCE(ingest_cursor.windows_dirty_from, CAST(:at AS timestamptz)),
        CAST(:at AS timestamptz)
    ),
    updated_at = now()
""")

DIRTY_CHANNELS = text("""
SELECT channel_id, windows_dirty_from
FROM ingest_cursor
WHERE windows_dirty_from IS NOT NULL
ORDER BY windows_dirty_from
LIMIT :limit
""")

CLEAR_WINDOWS_DIRTY = text("""
UPDATE ingest_cursor SET windows_dirty_from = NULL, updated_at = now()
WHERE channel_id = :channel_id
  AND windows_dirty_from IS NOT NULL
  AND windows_dirty_from <= CAST(:up_to AS timestamptz)
""")

MESSAGES_FOR_REWINDOW = text("""
SELECT m.id, m.channel_id, m.content, m.created_at, m.edited_at,
       m.reply_to_id, m.thread_id,
       COALESCE((
           SELECT p.platform_user_id FROM person_platform_id p
           WHERE p.person_id = m.author_person_id AND p.platform = :platform
           ORDER BY p.platform_user_id LIMIT 1
       ), m.author_person_id) AS platform_user_id
FROM message m
WHERE m.deleted_at IS NULL
  AND m.channel_id = :channel_id
  AND m.created_at >= CAST(:since AS timestamptz)
ORDER BY m.created_at, m.id
LIMIT :limit
""")

DELETE_WINDOWS_FROM = text("""
DELETE FROM conversation_window
WHERE channel_id = :channel_id AND ends_at >= CAST(:since AS timestamptz)
""")

# Closes the delete-during-rebuild race. A deletion landing between reading a
# channel's messages and writing its windows tombstones nothing, because no
# window contains the message yet -- and the rebuild then inserts a LIVE
# window carrying the retracted text. Re-applying tombstones inside the same
# transaction as the insert means such a window can never be observed live.
TOMBSTONE_WINDOWS_WITH_DEAD_MESSAGES = text("""
UPDATE conversation_window SET deleted_at = now()
WHERE deleted_at IS NULL
  AND channel_id = :channel_id
  AND id IN (
      SELECT wm.window_id FROM conversation_window_message wm
      JOIN message m ON m.id = wm.message_id
      WHERE m.deleted_at IS NOT NULL
  )
""")

EMBEDDINGS_FROM = text("""
SELECT text, embedding::text AS embedding
FROM conversation_window
WHERE channel_id = :channel_id
  AND ends_at >= CAST(:since AS timestamptz)
  AND embedding IS NOT NULL
""")
