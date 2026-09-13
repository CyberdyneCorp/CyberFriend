"""SQL for the document corpus, and where its permission predicate lives.

The rule is the same as for conversation, and the shape it takes here is the
only interesting design decision in this file.

A document can enter through several channels, so its visibility is a *set* of
channels rather than one. Storing that as a join table would put the
permission predicate on the other side of a join from the ranking, which is
precisely where pgvector stops being able to constrain its scan with it: an
approximate index would return its k best rows and the join would then remove
some, silently under-returning, worst for the people in the fewest channels.

So the entry channels are denormalised onto the chunk row as `channel_ids`,
and the predicate is a same-table array overlap bound into the same statement
as the ranking. The array is maintained from the live entries whenever they
change; `REFRESH_CHUNK_CHANNELS` is the one statement that writes it.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.sql.elements import TextClause

UPSERT_DOCUMENT = text("""
INSERT INTO document (
    identity_key, content_hash, origin, media_type, title, byte_size,
    external_uri, source_revision, metadata, fetched_at
) VALUES (
    :identity_key, :content_hash, :origin, :media_type, :title, :byte_size,
    CAST(:external_uri AS text), CAST(:source_revision AS text),
    CAST(:metadata AS jsonb), :fetched_at
)
ON CONFLICT (identity_key) DO UPDATE SET
    content_hash = EXCLUDED.content_hash,
    title = EXCLUDED.title,
    byte_size = EXCLUDED.byte_size,
    source_revision = EXCLUDED.source_revision,
    metadata = EXCLUDED.metadata,
    fetched_at = EXCLUDED.fetched_at
    -- deleted_at is deliberately absent: re-ingesting a document we withdrew
    -- must not resurrect it, exactly as for a tombstoned message.
RETURNING id
""")

DOCUMENT_CONTENT_HASH = text("""
SELECT content_hash FROM document WHERE identity_key = :identity_key AND deleted_at IS NULL
""")

RECORD_ENTRY = text("""
INSERT INTO document_entry (
    document_id, channel_id, message_id, uploader_person_id, attachment_id,
    source_url, entered_at
) VALUES (
    :document_id, :channel_id, :message_id, CAST(:uploader_person_id AS bigint),
    CAST(:attachment_id AS bigint), :source_url, :entered_at
)
ON CONFLICT (document_id, message_id, source_url) DO UPDATE SET
    -- Re-posting a link that was withdrawn is a fresh act of sharing.
    deleted_at = NULL,
    entered_at = EXCLUDED.entered_at
""")

# The single writer of the denormalised permission column. Everything that
# changes which channels a document entered through calls this afterwards.
REFRESH_CHUNK_CHANNELS = text("""
UPDATE document_chunk SET channel_ids = COALESCE((
    SELECT array_agg(DISTINCT e.channel_id)
    FROM document_entry e
    WHERE e.document_id = :document_id AND e.deleted_at IS NULL
), ARRAY[]::bigint[])
WHERE document_id = :document_id
""")

DELETE_CHUNKS = text("DELETE FROM document_chunk WHERE document_id = :document_id")

INSERT_CHUNK = text("""
INSERT INTO document_chunk (
    document_id, ordinal, text, location, heading, channel_ids, search_tsv
) VALUES (
    :document_id, :ordinal, :text, :location, CAST(:heading AS text),
    ARRAY[]::bigint[], to_tsvector('english', :text)
)
""")

TOMBSTONE_DOCUMENT = text("""
UPDATE document SET deleted_at = :at WHERE id = :document_id AND deleted_at IS NULL
""")

# Tombstoning the document tombstones its chunks in the same breath: the read
# path filters on the chunk row, so leaving the chunks live would keep
# returning withdrawn content.
TOMBSTONE_DOCUMENT_CHUNKS = text("""
UPDATE document_chunk SET deleted_at = :at
WHERE document_id = :document_id AND deleted_at IS NULL
""")

TOMBSTONE_ENTRIES_FOR_MESSAGE = text("""
UPDATE document_entry SET deleted_at = :at
WHERE message_id = :message_id AND deleted_at IS NULL
RETURNING document_id
""")

PURGE_CHANNEL_ENTRIES = text("""
DELETE FROM document_entry WHERE channel_id = :channel_id RETURNING document_id
""")

PURGE_PERSON_ENTRIES = text("""
DELETE FROM document_entry WHERE uploader_person_id = :person_id RETURNING document_id
""")

PURGE_ENTRIES_BEFORE = text("""
DELETE FROM document_entry WHERE entered_at < :cutoff RETURNING document_id
""")

# A document nobody shares any more is not retained for its own sake.
DELETE_ORPHANED_DOCUMENTS = text("""
DELETE FROM document d
WHERE NOT EXISTS (SELECT 1 FROM document_entry e WHERE e.document_id = d.id)
""")

RECORD_FETCH = text("""
INSERT INTO document_fetch (
    target, outcome, channel_id, message_id, document_hash, attempts, fetched_at
) VALUES (
    :target, :outcome, :channel_id, :message_id, CAST(:document_hash AS text),
    :attempts, :at
)
""")

# Only failures count towards the retry bound; a target that succeeded once is
# not held against itself when its content changes later.
FETCH_ATTEMPTS = text("""
SELECT COALESCE(MAX(attempts), 0) FROM document_fetch
WHERE target = :target AND outcome <> 'fetched'
""")

CHUNKS_MISSING_EMBEDDINGS = text("""
SELECT id, text FROM document_chunk
WHERE embedding IS NULL AND deleted_at IS NULL
ORDER BY id
LIMIT :limit
""")

STORE_CHUNK_EMBEDDING = text("""
UPDATE document_chunk SET embedding = CAST(:embedding AS vector) WHERE id = :id
""")

EXTERNAL_DOCUMENTS_DUE = text("""
SELECT id, identity_key, content_hash, origin, media_type, title, byte_size,
       external_uri, source_revision, fetched_at
FROM document
WHERE deleted_at IS NULL AND external_uri IS NOT NULL
ORDER BY fetched_at NULLS FIRST
LIMIT :limit
""")

# Documents and conversation counted apart, because one upload can produce
# more chunks than a month of the same channel's talk and a combined number
# hides which of the two is spending the money.
EMBEDDING_COST = text("""
SELECT
    (SELECT COUNT(*) FROM document WHERE deleted_at IS NULL) AS documents,
    (SELECT COUNT(*) FROM document_chunk WHERE deleted_at IS NULL) AS chunks,
    (SELECT COALESCE(SUM(length(text)), 0) FROM document_chunk
      WHERE deleted_at IS NULL) AS document_chars,
    (SELECT COUNT(*) FROM conversation_window WHERE deleted_at IS NULL) AS windows,
    (SELECT COALESCE(SUM(length(text)), 0) FROM conversation_window
      WHERE deleted_at IS NULL) AS window_chars
""")

# --- retrieval ---------------------------------------------------------
#
# `:channel_ids` is the viewer's readable set, bound into the same statement
# as the ranking. `channel_ids && :channel_ids` is "entered through any
# channel this viewer may read", which is exactly the visibility rule: each
# share is an independent disclosure.
#
# The lateral join picks the entry a citation should point at. It re-applies
# the channel check, which is redundant with the overlap predicate and meant
# to be: it catches a stale denormalised array rather than trusting it. Rows
# it removes are rows that should never have matched, which is the opposite
# of the post-filtering failure this file exists to avoid.

_ENTRY_JOIN = """
LEFT JOIN LATERAL (
    SELECT e.channel_id, e.message_id, e.source_url
    FROM document_entry e
    WHERE e.document_id = c.document_id
      AND e.deleted_at IS NULL
      AND e.channel_id = ANY(:channel_ids)
    ORDER BY e.entered_at
    LIMIT 1
) entry ON TRUE
"""

LEXICAL_DOCUMENT_SEARCH = text(f"""
SELECT c.id, c.document_id, c.text, c.location, c.heading, d.title,
       entry.channel_id, entry.message_id, entry.source_url,
       ts_rank_cd(c.search_tsv, plainto_tsquery('english', :q)) AS score
FROM document_chunk c
JOIN document d ON d.id = c.document_id
{_ENTRY_JOIN}
WHERE c.deleted_at IS NULL
  AND d.deleted_at IS NULL
  AND c.channel_ids && CAST(:channel_ids AS bigint[])
  AND c.search_tsv @@ plainto_tsquery('english', :q)
  AND entry.channel_id IS NOT NULL
ORDER BY score DESC
LIMIT :limit
""")

VECTOR_DOCUMENT_SEARCH = text(f"""
SELECT c.id, c.document_id, c.text, c.location, c.heading, d.title,
       entry.channel_id, entry.message_id, entry.source_url,
       1 - (c.embedding <=> CAST(:embedding AS vector)) AS score
FROM document_chunk c
JOIN document d ON d.id = c.document_id
{_ENTRY_JOIN}
WHERE c.deleted_at IS NULL
  AND d.deleted_at IS NULL
  AND c.embedding IS NOT NULL
  AND c.channel_ids && CAST(:channel_ids AS bigint[])
  AND entry.channel_id IS NOT NULL
ORDER BY c.embedding <=> CAST(:embedding AS vector)
LIMIT :limit
""")


def statements() -> Sequence[TextClause]:
    """Every content-returning statement, for the test that audits them."""
    return (LEXICAL_DOCUMENT_SEARCH, VECTOR_DOCUMENT_SEARCH)
