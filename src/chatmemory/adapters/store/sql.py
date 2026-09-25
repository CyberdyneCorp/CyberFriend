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
    reply_to_id, thread_id, search_tsv, deleted_at
) VALUES (
    :id, :channel_id, :author_person_id, :content, :created_at, :edited_at,
    :reply_to_id, :thread_id, to_tsvector('english', :content),
    -- A message is born withdrawn when the ledger already holds a tombstone
    -- for its id. Live messages are queued and written asynchronously while a
    -- deletion goes straight to the database, so the delete routinely lands
    -- first; reading the verdict here is what makes the insert correct for
    -- either arrival order rather than for the lucky one.
    (SELECT t.deleted_at FROM message_tombstone t WHERE t.message_id = :id)
)
ON CONFLICT (id) DO UPDATE SET
    content = EXCLUDED.content,
    edited_at = EXCLUDED.edited_at,
    search_tsv = EXCLUDED.search_tsv,
    -- A new content revision, which is a message the extractor has not read.
    -- Bumped here because this clause is the one place message text changes:
    -- an edit that only re-marked the message pending from the caller would
    -- be pending exactly as often as somebody remembered to mark it.
    asks_extraction_seq = message.asks_extraction_seq + 1,
    -- An edit un-deletes nothing, but a re-ingest of a live message must not
    -- resurrect a tombstone either.
    deleted_at = message.deleted_at
WHERE message.edited_at IS DISTINCT FROM EXCLUDED.edited_at
   OR message.content IS DISTINCT FROM EXCLUDED.content
""")

# Taken by both the capture path and the deletion path, for the length of
# their transaction, keyed on the platform message id.
#
# The ledger below fixes arrival order; this fixes overlap. Without it a
# capture that has not yet committed is invisible to a concurrent deletion's
# UPDATE, while that deletion is equally invisible to the capture's read of
# the ledger -- and the message commits live with its own tombstone already
# recorded. Holding the lock makes one of the two orderings a fact.
LOCK_MESSAGE = text("SELECT pg_advisory_xact_lock(CAST(:id AS bigint))")

# The deletion ledger: what was withdrawn, keyed on the platform message id
# alone. Deliberately references no row in `message` -- recording a tombstone
# for a message the corpus has never seen is the entire point, and an UPDATE
# matching zero rows is exactly how retracted content used to stay live
# forever.
RECORD_TOMBSTONE = text("""
INSERT INTO message_tombstone (message_id, channel_id, deleted_at)
VALUES (
    :id,
    -- Known only once the message itself has landed; NULL until then.
    (SELECT m.channel_id FROM message m WHERE m.id = :id),
    :at
)
ON CONFLICT (message_id) DO UPDATE SET
    channel_id = COALESCE(message_tombstone.channel_id, EXCLUDED.channel_id),
    -- Earliest wins: a deletion re-reported by a later reconciliation pass
    -- must not move the withdrawal forward past when it actually happened.
    deleted_at = LEAST(message_tombstone.deleted_at, EXCLUDED.deleted_at)
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
WITH w AS (DELETE FROM conversation_window WHERE channel_id = :channel_id RETURNING id),
     -- The ledger goes with the content it describes. Nothing can be
     -- re-ingested into a de-scoped channel (capture refuses it), and if the
     -- channel returns to scope its backfill re-reads live history, which no
     -- longer contains the deleted messages -- so this cannot resurrect one.
     t AS (DELETE FROM message_tombstone WHERE channel_id = :channel_id RETURNING message_id),
     -- And so does the backfill cursor. It only ever moves older (LEAST), so
     -- one left behind points at the oldest message already imported: a
     -- channel returning to scope would resume from there, find nothing
     -- older, report itself complete, and never re-import the history just
     -- deleted. The dirty-window watermark on the same row describes windows
     -- this statement deletes, so nothing on the row outlives the purge.
     c AS (DELETE FROM ingest_cursor WHERE channel_id = :channel_id RETURNING channel_id)
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
       ), m.author_person_id) AS platform_user_id,
       -- The name a reader recognises. Windowing renders it into the window
       -- text, which is what gets embedded and what a citation excerpt
       -- shows, so a rebuild that could not resolve it would silently put
       -- account ids back into both.
       (SELECT pe.display_name FROM person pe WHERE pe.id = m.author_person_id)
           AS author_display
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

# --- reconciliation ----------------------------------------------------
#
# What the corpus holds, as revisions rather than as content. Reconciliation
# compares this against a re-read of live history: a changed revision is the
# only evidence of an edit we missed, and absence the only evidence of a
# deletion, because a gateway event fired while the process was down is never
# replayed.
#
# No viewer is bound, and none can be: this runs for the ingest process, which
# acts for nobody. It is safe to be the exception because it returns
# timestamps and ids -- never a word of message text -- so it cannot become a
# way to read the corpus unfiltered.
STORED_REVISIONS = text("""
SELECT m.id, COALESCE(m.edited_at, m.created_at) AS revision
FROM message m
WHERE m.channel_id = :channel_id
  -- Tombstoned messages are already withdrawn. Reporting them would make
  -- every pass rediscover the same deletion and re-apply it forever.
  AND m.deleted_at IS NULL
  AND m.created_at >= CAST(:since AS timestamptz)
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
SELECT m.id, m.channel_id, m.content, m.created_at,
       m.edited_at, m.reply_to_id, m.thread_id,
       -- The platform account, not the internal person row id: callers hand
       -- this out as a Discord user id. Scalar subquery for the same reason
       -- as MESSAGES_WITHOUT_WINDOW (a person may carry several accounts).
       COALESCE((
           SELECT p.platform_user_id FROM person_platform_id p
           WHERE p.person_id = m.author_person_id AND p.platform = :platform
           ORDER BY p.platform_user_id LIMIT 1
       ), m.author_person_id) AS platform_user_id
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


# What one person said: their own messages, not the windows around them.
#
# The viewer's channels, tombstones, the author and the span are all in the
# WHERE, so the person filter can only narrow what the viewer may read. The
# topic is ranked over every one of those rows, by exact cosine against each
# message's window (no approximate scan, so nothing is under-returned), with
# the message's own lexical rank breaking ties and recency after that; only
# then are the best :candidates kept. Ranking before the cut is what lets a
# prolific person's old on-topic message beat two hundred newer off-topic
# ones. A NULL :embedding -- no topic -- leaves every score NULL and the order
# chronological. A message not yet windowed has no window to cite and is left
# out until ingest windows it.
AUTHOR_SEARCH = text("""
SELECT m.id AS message_id, m.channel_id, m.content, m.created_at,
       pe.display_name AS author_display,
       w.id AS window_id, w.starts_at, w.ends_at,
       1 - (w.embedding <=> CAST(:embedding AS vector)) AS score
FROM message m
JOIN person pe ON pe.id = m.author_person_id
JOIN conversation_window_message cwm ON cwm.message_id = m.id
JOIN conversation_window w ON w.id = cwm.window_id
WHERE m.deleted_at IS NULL
  AND w.deleted_at IS NULL
  AND m.channel_id = ANY(:channel_ids)
  AND w.channel_id = ANY(:channel_ids)
  AND m.author_person_id IN (
      SELECT a.person_id FROM person_platform_id a
      WHERE a.platform = :platform AND a.platform_user_id = ANY(:author_ids)
  )
  AND (CAST(:since AS timestamptz) IS NULL OR m.created_at >= CAST(:since AS timestamptz))
  AND (CAST(:until AS timestamptz) IS NULL OR m.created_at < CAST(:until AS timestamptz))
ORDER BY score DESC NULLS LAST,
         ts_rank_cd(m.search_tsv, plainto_tsquery('english', :q)) DESC,
         m.created_at DESC
LIMIT :candidates
""")


# Who a typed name may be, among people the viewer can see speak. A person is
# a candidate only with a live message in the viewer's channels, so the list
# never names somebody known only from a channel the viewer may not read. The
# LIKE on the folded first word is a cheap narrowing; which of these the name
# actually means is decided in `app.people`.
PEOPLE_VISIBLE = text("""
SELECT p.display_name,
       (SELECT min(a.platform_user_id) FROM person_platform_id a
        WHERE a.person_id = p.id AND a.platform = :platform) AS platform_user_id
FROM person p
WHERE translate(lower(p.display_name), :accented, :plain) LIKE :pattern
  AND EXISTS (
      SELECT 1 FROM message m
      WHERE m.author_person_id = p.id
        AND m.deleted_at IS NULL
        AND m.channel_id = ANY(:channel_ids)
  )
ORDER BY p.display_name
LIMIT :cap
""")


def statements() -> Sequence[TextClause]:
    """Every content-returning statement, for the test that audits them."""
    return (
        LEXICAL_SEARCH, VECTOR_SEARCH, THREAD_CONTEXT, LIST_CHANNELS, AUTHOR_SEARCH,
        PEOPLE_VISIBLE,
    )


# --- window rebuild scheduling -----------------------------------------
#
# A channel is marked dirty from the moment its content changed; the rebuild
# re-forms every window from there. Keyed on the EARLIEST pending change, so
# a later edit cannot move the watermark forward past work not yet done.

MARK_WINDOWS_DIRTY = text("""
INSERT INTO ingest_cursor (channel_id, windows_dirty_from, windows_dirty_seq)
VALUES (:channel_id, CAST(:at AS timestamptz), 1)
ON CONFLICT (channel_id) DO UPDATE SET
    windows_dirty_from = LEAST(
        COALESCE(ingest_cursor.windows_dirty_from, CAST(:at AS timestamptz)),
        CAST(:at AS timestamptz)
    ),
    -- Bumped on every mark. The clear is conditional on it, so a change that
    -- arrives mid-rebuild is not swallowed by the clear that follows: the
    -- watermark itself does not move, because marks fold in with LEAST.
    windows_dirty_seq = ingest_cursor.windows_dirty_seq + 1,
    updated_at = now()
""")

# The same watermark, derived from the message rather than passed in. A
# deletion is a content change like any other: tombstoning the window that
# held the message withdraws its surviving neighbours too, so the channel has
# to be re-formed or one deletion quietly takes a whole conversation out of
# retrieval. Derived here because not every caller knows the channel --
# reconciliation discovers a deletion by absence from history and holds only
# an id -- and a rebuild that depends on the caller remembering is a rebuild
# that will be forgotten.
MARK_DIRTY_FOR_MESSAGE = text("""
INSERT INTO ingest_cursor (channel_id, windows_dirty_from)
SELECT m.channel_id, m.created_at FROM message m WHERE m.id = :id
ON CONFLICT (channel_id) DO UPDATE SET
    windows_dirty_from = LEAST(
        COALESCE(ingest_cursor.windows_dirty_from, EXCLUDED.windows_dirty_from),
        EXCLUDED.windows_dirty_from
    ),
    updated_at = now()
""")

DIRTY_CHANNELS = text("""
SELECT channel_id, windows_dirty_from, windows_dirty_seq
FROM ingest_cursor
WHERE windows_dirty_from IS NOT NULL
ORDER BY windows_dirty_from
LIMIT :limit
""")

CLEAR_WINDOWS_DIRTY = text("""
UPDATE ingest_cursor SET windows_dirty_from = NULL, updated_at = now()
WHERE channel_id = :channel_id
  AND windows_dirty_from IS NOT NULL
  -- Only when nothing was marked since the rebuild read this row. Otherwise
  -- a change racing the rebuild is cleared without ever being applied.
  AND windows_dirty_seq = :seq
""")

MESSAGES_FOR_REWINDOW = text("""
SELECT m.id, m.channel_id, m.content, m.created_at, m.edited_at,
       m.reply_to_id, m.thread_id,
       COALESCE((
           SELECT p.platform_user_id FROM person_platform_id p
           WHERE p.person_id = m.author_person_id AND p.platform = :platform
           ORDER BY p.platform_user_id LIMIT 1
       ), m.author_person_id) AS platform_user_id,
       -- The name a reader recognises. Windowing renders it into the window
       -- text, which is what gets embedded and what a citation excerpt
       -- shows, so a rebuild that could not resolve it would silently put
       -- account ids back into both.
       (SELECT pe.display_name FROM person pe WHERE pe.id = m.author_person_id)
           AS author_display
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

# A message carries a foreign key to its channel, and nothing else creates
# that row: the channel is discovered by ingesting from it, not configured
# in advance. Every integration test inserted one by hand, which is exactly
# why this went unnoticed until a real deploy.
ENSURE_CHANNEL = text("""
INSERT INTO channel (id, platform, name, is_indexed)
VALUES (:id, :platform, :name, TRUE)
ON CONFLICT (id) DO UPDATE SET
    -- Keep a name once we learn one, but never overwrite it with a blank.
    name = COALESCE(NULLIF(EXCLUDED.name, ''), channel.name)
""")

# People still called by their account id: everyone whose messages were
# captured before names were written on ingest (2f95d17). A backfilled message
# is never captured again, so without a repair they stay unnamed for good and
# "o que o João disse" can never find João.
UNNAMED_PEOPLE = text("""
SELECT a.platform_user_id
FROM person p
JOIN person_platform_id a ON a.person_id = p.id AND a.platform = :platform
WHERE p.display_name = a.platform_user_id::text
ORDER BY p.id
LIMIT :cap
""")

UPDATE_PERSON_DISPLAY = text("""
UPDATE person SET display_name = :n
WHERE id = :id AND display_name IS DISTINCT FROM :n
""")


# --- ask extraction scheduling ------------------------------------------
#
# Extraction is billed per message, so the unit of work here is a message and
# not a channel. A per-channel watermark would be wrong in the direction that
# costs money: backfill walks history newest-first, so every page imported
# moves the watermark backwards, and the next pass would pay for a model call
# on everything newer than it all over again.
#
# The generation is migration 0009's mechanism applied per message, and it is
# here for the same race. Extraction takes a model call and seconds; an edit
# landing in that gap must not be swallowed by the mark that follows it.
# `asks_extraction_seq` counts content revisions and is bumped by the upsert
# above; `asks_extracted_seq` is the revision that was actually read. Pending
# is the two differing, so recording a generation the message has since moved
# past leaves it pending rather than clearing work never done -- which also
# means the mark cannot be written without the generation in hand.

MESSAGES_PENDING_EXTRACTION = text("""
SELECT m.id, m.channel_id, m.content, m.created_at, m.edited_at,
       m.reply_to_id, m.thread_id, m.asks_extraction_seq,
       COALESCE((
           SELECT p.platform_user_id FROM person_platform_id p
           WHERE p.person_id = m.author_person_id AND p.platform = :platform
           ORDER BY p.platform_user_id LIMIT 1
       ), m.author_person_id) AS platform_user_id,
       -- The name the directory learns people by, so a backfilled ask that
       -- names somebody in prose can still resolve to them.
       (SELECT pe.display_name FROM person pe WHERE pe.id = m.author_person_id)
           AS author_display,
       -- Mentions decide both whether a message is worth a model call at all
       -- and who the ask fell to. The live path is handed them by the
       -- gateway; a message read back out of the corpus has to bring its own,
       -- or every backfilled "@bob can you..." arrives looking like chatter.
       COALESCE((
           SELECT array_agg(DISTINCT pp.platform_user_id)
           FROM message_mention mm
           JOIN person_platform_id pp
             ON pp.person_id = mm.person_id AND pp.platform = :platform
           WHERE mm.message_id = m.id
       ), ARRAY[]::bigint[]) AS mention_ids
FROM message m
JOIN channel c ON c.id = m.channel_id
WHERE m.deleted_at IS NULL
  AND m.asks_extracted_seq IS DISTINCT FROM m.asks_extraction_seq
  -- Indexing scope, bound from the operator's configuration rather than read
  -- from `channel.is_indexed`. That column is written TRUE when a channel is
  -- first seen and never written again by anything, so a predicate on it
  -- excludes nothing: a channel removed from scope would go on being read and
  -- paid for. Binding the ids is also how every viewer-scoped read works.
  AND m.channel_id = ANY(:indexed_channel_ids)
  -- Young messages belong to the live pass. One captured seconds ago is
  -- probably still buffered in the extraction worker's window and about to be
  -- extracted from there, so reading it here as well buys the same model call
  -- twice. Anything the live pass drops or fails on arrives here a few minutes
  -- later, and history is older than this by many orders of magnitude.
  AND m.created_at < now() - INTERVAL '5 minutes'
-- Newest first: the obligations somebody still cares about are the recent
-- ones, and history imports newest-first too, so the backlog drains in the
-- order it becomes answerable.
ORDER BY m.created_at DESC
LIMIT :limit
""")

# The conversation the live pass would have shown the model beside each
# message: the `:limit` live messages before it in its channel, and its reply
# parent. The backlog pass needs it because it reads only what is pending, and
# after an edit that is the edited message alone. Same channel only, for both,
# which also keeps the read inside the scope the pending messages came from.
EXTRACTION_CONTEXT = text("""
WITH target AS (
    SELECT m.id, m.channel_id, m.created_at, m.reply_to_id
    FROM message m
    WHERE m.id = ANY(CAST(:ids AS bigint[]))
),
shown AS (
    SELECT t.id AS context_for, 'preceding' AS role, p.id AS message_id
    FROM target t
    CROSS JOIN LATERAL (
        SELECT b.id FROM message b
        WHERE b.channel_id = t.channel_id
          AND b.deleted_at IS NULL
          AND (b.created_at, b.id) < (t.created_at, t.id)
        ORDER BY b.created_at DESC, b.id DESC
        LIMIT :limit
    ) p
    UNION ALL
    SELECT t.id, 'parent', p.id
    FROM target t
    JOIN message p ON p.id = t.reply_to_id AND p.channel_id = t.channel_id
    WHERE p.deleted_at IS NULL
)
SELECT s.context_for, s.role,
       m.id, m.channel_id, m.content, m.created_at, m.edited_at,
       m.reply_to_id, m.thread_id,
       COALESCE((
           SELECT pp.platform_user_id FROM person_platform_id pp
           WHERE pp.person_id = m.author_person_id AND pp.platform = :platform
           ORDER BY pp.platform_user_id LIMIT 1
       ), m.author_person_id) AS platform_user_id,
       (SELECT pe.display_name FROM person pe WHERE pe.id = m.author_person_id)
           AS author_display
FROM shown s
JOIN message m ON m.id = s.message_id
""")

RECORD_EXTRACTION = text("""
UPDATE message m
-- COALESCE because the live path has no generation to give: capture hands it
-- a message rather than reading a row, so it records whatever revision is
-- current. That is no weaker than the behaviour it replaces -- a live message
-- was extracted exactly once and never revisited -- and it is what keeps the
-- backlog pass from paying a second time for everything the stream delivered.
SET asks_extracted_seq = COALESCE(b.seq, m.asks_extraction_seq)
FROM unnest(CAST(:ids AS bigint[]), CAST(:generations AS bigint[])) AS b(id, seq)
WHERE m.id = b.id
""")

# Capped on purpose. The number a reader needs is "is the backlog draining",
# which "1000+" answers as well as an exact count does, and the exact count is
# a scan of every pending row on a corpus that may have millions of them.
PENDING_EXTRACTION_COUNT = text("""
SELECT count(*) FROM (
    SELECT 1 FROM message m
    JOIN channel c ON c.id = m.channel_id
    WHERE m.deleted_at IS NULL
      AND m.asks_extracted_seq IS DISTINCT FROM m.asks_extraction_seq
      -- The same predicates the pass itself reads on, or the figure reports a
      -- backlog that is never going to drain because nothing will read it.
      -- Scope comes from the operator's configuration: `channel.is_indexed`
      -- is set TRUE once and never unset, so reading it excludes nothing.
      AND m.channel_id = ANY(:indexed_channel_ids)
      AND m.created_at < now() - INTERVAL '5 minutes'
    LIMIT :cap
) pending
""")
