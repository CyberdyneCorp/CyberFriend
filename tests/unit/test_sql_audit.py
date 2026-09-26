"""The SQL audit, derived from the SQL instead of from a list kept beside it.

The previous audit checked the statements someone had remembered to add to a
dict, and completeness was asserted against `sql.statements()` -- a second
hand-maintained list. Three content-returning SELECTs were added to `sql`
outside both, and the module docstring was amended to describe them as an
exempt category. Nothing failed. Catching that is the one thing this file
exists to do, so it no longer asks anyone to remember anything:

*   every `TextClause` bound at module level in the store's SQL modules is
    found by reflection, not by enumeration;
*   each one must either bind `:channel_ids` -- the viewer's readable set --
    or appear in `UNSCOPED` below with a written reason;
*   a reason that is empty, or a registration for a statement that no longer
    exists, fails as loudly as an unregistered statement does.

Adding a statement and not doing one of those two things fails this file by
construction, which is what "the audit cannot be bypassed" has to mean. The
registry is verbose on purpose: writing down why a statement may run without a
viewer is the review, and a category exemption is exactly what removed it last
time.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping

import pytest
from sqlalchemy import text
from sqlalchemy.sql.elements import TextClause

SQL_MODULES = (
    "sql",
    "asks_sql",
    "decisions_sql",
    "notify_sql",
    "documents_sql",
    "retention_sql",
    "admin_sql",
    "config_sql",
    "memory_sql",
    "facts_sql",
    "media_sql",
    "feature_requests_sql",
    "privacy_sql",
    "erasure_sql",
    "tracing_notice_sql",
)

VIEWER_BIND = ":channel_ids"


def discover() -> dict[str, TextClause]:
    """Every module-level statement in the store's SQL modules, by qualified name.

    Reflection rather than a registry: a statement is audited because it
    exists, not because it was listed. Statements re-exported between modules
    are attributed to the module that defines them, so reusing one does not
    create a second entry that has to be registered twice.
    """
    found: dict[str, TextClause] = {}
    seen: set[int] = set()
    for module_name in SQL_MODULES:
        module = importlib.import_module(f"chatmemory.adapters.store.{module_name}")
        for attribute, value in vars(module).items():
            if isinstance(value, TextClause) and id(value) not in seen:
                seen.add(id(value))
                found[f"{module_name}.{attribute}"] = value
    return found


STATEMENTS = discover()

# Statements that run without a viewer, and why each one may. "Why" is the
# point: a reason has to be written by a person, and writing "returns the text
# of other people's conversations to whoever asks" is hard to do by accident.
UNSCOPED: dict[str, str] = {
    # --- sql: writes -----------------------------------------------------
    "sql.UPDATE_PERSON_DISPLAY": "write; keeps a person's shown name current",
    "sql.UNNAMED_PEOPLE": (
        "ingest maintenance; returns account ids only, never content, to be named from Discord"
    ),
    "sql.ENSURE_CHANNEL": (
        "write; registers the channel a message belongs to. Returns no row, "
        "and cannot be viewer-scoped: it runs during ingestion, where there "
        "is no viewer, and the channel it names is the one being ingested"
    ),
    "sql.UPSERT_MESSAGE": "write; stores one message and returns no row",
    "sql.LOCK_MESSAGE": "advisory lock; reads no table",
    "sql.RECORD_TOMBSTONE": "write; the deletion ledger",
    "sql.TOMBSTONE_MESSAGE": "write; withdraws content",
    "sql.TOMBSTONE_WINDOWS_FOR_MESSAGE": "write; withdraws content",
    "sql.PURGE_CHANNEL": "write; removes a channel that left indexing scope",
    "sql.UPSERT_MEDIA": (
        "write; one pending media row for an attachment of the message being "
        "captured. Returns no row"
    ),
    "sql.DROP_UNATTACHED_MEDIA": (
        "write; removes the rows of attachments an edit took off the message "
        "being captured"
    ),
    "sql.WITHDRAW_MEDIA_FOR_MESSAGE": "write; withdraws content",
    "sql.DELETE_WINDOWS_FOR_MESSAGES": "write; supersedes windows during a rebuild",
    "sql.INSERT_WINDOW": "write; returns the new window's id only",
    "sql.INSERT_WINDOW_MESSAGES": "write; membership rows",
    "sql.STORE_EMBEDDING": "write; stores a vector",
    "sql.SET_DISPLAY_NAME": "write; person metadata",
    "sql.MARK_WINDOWS_DIRTY": "write; rebuild watermark",
    "sql.MARK_DIRTY_FOR_MESSAGE": "write; rebuild watermark",
    "sql.CLEAR_WINDOWS_DIRTY": "write; rebuild watermark",
    "sql.DELETE_WINDOWS_FROM": "write; supersedes windows during a rebuild",
    "sql.TOMBSTONE_WINDOWS_WITH_DEAD_MESSAGES": "write; withdraws content",
    # --- sql: reads that are not content ---------------------------------
    "sql.WINDOW_MESSAGE_IDS": (
        "ids only; maps windows a viewer-scoped search already returned to "
        "their message ids"
    ),
    "sql.DIRTY_CHANNELS": "scheduling metadata; a channel id and a watermark",
    "sql.RECORD_EXTRACTION": "write; the ask-extraction watermark",
    "sql.PENDING_EXTRACTION_COUNT": (
        "write-side read with no viewer; extraction runs in the ingest\n"
        "process, where there is no requester. Bounded by the operator's\n"
        "indexed scope via :indexed_channel_ids -- deliberately not\n"
        ":channel_ids, which in every other statement means what one\n"
        "viewer may read"
    ),
    "sql.STORED_REVISIONS": (
        "ingest-only; message ids and revision timestamps for reconciliation, "
        "no message text"
    ),
    # --- sql: content returned to the ingestion process ------------------
    #
    # These return text with no viewer bound. They run for windowing,
    # embedding and ask extraction, which act for nobody: there is no viewer
    # to bind, and doing only the channels some person may read would leave
    # the rest of the corpus permanently unwindowed and therefore permanently
    # unretrievable. None of them is reachable from a request-scoped surface;
    # the entrypoint that drives them serves no requests.
    "sql.MESSAGES_WITHOUT_WINDOW": (
        "ingest-only; message text for the windowing worker, which acts for "
        "nobody. Unreachable from any request-scoped surface"
    ),
    "sql.MESSAGES_FOR_REWINDOW": (
        "ingest-only; message text for the channel rebuild, which acts for "
        "nobody. Unreachable from any request-scoped surface"
    ),
    "sql.MESSAGES_PENDING_EXTRACTION": (
        "write-side read with no viewer; extraction runs in the ingest\n"
        "process, where there is no requester. Bounded by the operator's\n"
        "indexed scope via :indexed_channel_ids -- deliberately not\n"
        ":channel_ids, which in every other statement means what one\n"
        "viewer may read"
    ),
    "sql.EXTRACTION_CONTEXT": (
        "ingest-only; the messages around ones the extraction pass already\n"
        "read, in the same channel, so the model sees the conversation the\n"
        "live pass would have shown it. Acts for nobody, and unreachable\n"
        "from any request-scoped surface"
    ),
    "sql.WINDOWS_MISSING_EMBEDDINGS": (
        "ingest-only; window text for the embedding worker. Unreachable from "
        "any request-scoped surface"
    ),
    "sql.SUPERSEDED_EMBEDDINGS": (
        "ingest-only; window text keyed to its vector so a rebuild that "
        "changes nothing keeps it. Unreachable from any request-scoped surface"
    ),
    "sql.EMBEDDINGS_FROM": (
        "ingest-only; window text keyed to its vector, as SUPERSEDED_EMBEDDINGS"
    ),
    # --- asks_sql --------------------------------------------------------
    "asks_sql.RESOLVE_PERSON_ID": "identity lookup; a person id, no content",
    "asks_sql.UPSERT_ASK": "write; stores an extracted ask",
    "asks_sql.PRUNE_ASKS": "write; withdraws asks a re-run no longer finds",
    "asks_sql.RECORD_REACTION": "write; an observed reaction",
    # --- decisions_sql: writes only until the read path exists -------------
    "decisions_sql.RESOLVE_PERSON_ID": "identity lookup; a person id, no content",
    "decisions_sql.UPSERT_DECISION": "write; stores an extracted decision",
    "decisions_sql.PRUNE_DECISIONS": "write; withdraws decisions a re-run no longer finds",
    "decisions_sql.WITHDRAW_DECISIONS": (
        "write; withdraws decisions from messages a pass no longer sends to the model"
    ),
    "decisions_sql.WITHDRAW_MESSAGE_DECISIONS": (
        "write; withdraws decisions resting on a message somebody deleted"
    ),
    "decisions_sql.BACKFILL_SCAN": (
        "operator CLI only (`just decisions-backfill`); bound to the configured "
        "indexing scope, and the text is matched against DECISION_MARKERS "
        "in-process and discarded, never shown to anybody"
    ),
    "decisions_sql.BACKFILL_RESET": "write; puts marker-bearing history back in the queue",
    "asks_sql.CLOSE_ANSWERED_BY_REPLY": "write; state transition from an event",
    "asks_sql.CLOSE_ANSWERED_BY_REACTION": "write; state transition from an event",
    "asks_sql.CLOSE_ANSWERED_BY_REACTION_FOR_MESSAGE": (
        "write; the same closing rule as the periodic pass, narrowed to one "
        "message so a tick closes what it answers immediately. No viewer: an "
        "ask is closed by its own addressee, bound as a predicate"
    ),
    "asks_sql.REMOVE_REACTION": (
        "write; withdraws a reaction the same person left. No viewer: the "
        "actor is bound as a predicate, and a reaction is not content"
    ),
    "asks_sql.REOPEN_WITHOUT_REACTION": (
        "write; undoes a closure whose acknowledgement has been removed. No "
        "viewer: it acts on the ask whose addressee left the reaction"
    ),
    "asks_sql.MARK_STALE": "write; ageing, which never closes an ask",
    # --- notify_sql ------------------------------------------------------
    #
    # The one statement here that returns content -- PENDING_FOR_VIEWER -- is
    # not registered below, because it binds :channel_ids like every other
    # read in the system. The difference is *when*: the set it binds is
    # resolved from live guild state immediately before a message is sent,
    # not when the obligation was extracted, which is the whole of the
    # send-time permission re-check. SETTLE_UNREADABLE binds it too, as that
    # statement's complement, and is therefore scoped rather than registered.
    # Everything registered here is a write or a count of rows.
    "notify_sql.RESOLVE_PERSON_ID": "identity lookup; a person id, no content",
    "notify_sql.QUEUE_OBLIGATIONS": (
        "write; queues a notification for an ask addressed to one person. No\n"
        "viewer: it runs in the ingest process, where there is no requester,\n"
        "and it returns no row. Its own predicates are the bounds -- an\n"
        "individual addressee, not the requester, not opted out, not\n"
        "corrected, above the presentation threshold, and recent"
    ),
    "notify_sql.WITHDRAW_SETTLED": (
        "write; settles queued notifications whose ask was answered or "
        "corrected before anybody was told"
    ),
    "notify_sql.EXPIRE_PENDING": (
        "write; settles queued notifications too old to be news"
    ),
    "notify_sql.RECIPIENTS_DUE": (
        "identities only; who is owed a batch, as a person id and a platform\n"
        "account. Deliberately unscoped: this is the step *before* a viewer\n"
        "exists, and it returns no channel, no ask and no message text. What\n"
        "each person is told is decided by PENDING_FOR_VIEWER, under the\n"
        "viewer resolved from live guild state a moment later"
    ),
    "notify_sql.MARK_SENT": "write; records delivery of the rows just sent",
    "notify_sql.RECORD_SENT": (
        "write; the rate-limit stamp and the record that this person has been "
        "messaged once, which is what makes the first message say how to stop"
    ),
    "notify_sql.RECORD_ATTEMPT": (
        "write; stamps a delivery that was attempted and failed, so the rate\n"
        "limit bounds attempts and not only successes. No content, and no\n"
        "`first_notified_at`: a failed send is no evidence that anybody was\n"
        "ever messaged, so the first message that lands still says how to stop"
    ),
    "notify_sql.RECORD_UNDELIVERABLE": (
        "write; records that a person's direct messages are closed, so they "
        "are excluded from every later claim"
    ),
    "notify_sql.SETTLE_UNDELIVERABLE": (
        "write; empties the queue of somebody who cannot be reached"
    ),
    "notify_sql.SET_ENABLED": (
        "write; one person's own switch, keyed on the person id resolved from\n"
        "their authenticated account. Returns their preference and nothing\n"
        "else -- there is no path here that reads anybody else's"
    ),
    "notify_sql.READ_PREFERENCE": (
        "the caller's own preference: two booleans, keyed on the person id "
        "resolved from their authenticated account. No content"
    ),
    "notify_sql.QUEUE_DEPTH": "a bounded count for the health endpoint, no content",
    # --- documents_sql ---------------------------------------------------
    "documents_sql.UPSERT_DOCUMENT": "write; returns the document id only",
    "documents_sql.DOCUMENT_CONTENT_HASH": "a hash; lets an unchanged document skip parsing",
    "documents_sql.RECORD_ENTRY": "write; records one act of sharing",
    "documents_sql.REFRESH_CHUNK_CHANNELS": "write; the permission column itself",
    "documents_sql.DELETE_CHUNKS": "write; discards a document's previous chunks",
    "documents_sql.INSERT_CHUNK": "write; stores one chunk",
    "documents_sql.TOMBSTONE_DOCUMENT": "write; withdraws content",
    "documents_sql.TOMBSTONE_DOCUMENT_CHUNKS": "write; withdraws content",
    "documents_sql.TOMBSTONE_ENTRIES_FOR_MESSAGE": "write; returns document ids only",
    "documents_sql.PURGE_CHANNEL_ENTRIES": "write; returns document ids only",
    "documents_sql.PURGE_PERSON_ENTRIES": "write; opt-out, returns document ids only",
    "documents_sql.PURGE_ENTRIES_BEFORE": "write; retention, returns document ids only",
    "documents_sql.DELETE_ORPHANED_DOCUMENTS": "write; removes unshared documents",
    "documents_sql.RECORD_FETCH": "write; the fetch audit trail",
    "documents_sql.FETCH_ATTEMPTS": "a count; bounds retries",
    "documents_sql.STORE_CHUNK_EMBEDDING": "write; stores a vector",
    "documents_sql.CHUNKS_MISSING_EMBEDDINGS": (
        "ingest-only; chunk text for the embedding worker. Unreachable from "
        "any request-scoped surface"
    ),
    "documents_sql.EXTERNAL_DOCUMENTS_DUE": (
        "ingest-only; document metadata for the reconciliation worker, no "
        "chunk text"
    ),
    "documents_sql.EMBEDDING_COST": "aggregate counts and character totals, no content",
    # --- retention_sql ---------------------------------------------------
    "retention_sql.PERSON_BY_PLATFORM_ID": "identity lookup; a person id, no content",
    "retention_sql.CREATE_PERSON": "write; returns the new person id only",
    "retention_sql.LINK_PLATFORM_ID": "write; identity mapping",
    "retention_sql.PURGE_WINDOWS_BEFORE": "write; retention, returns channel ids only",
    "retention_sql.PURGE_MESSAGES_BEFORE": "write; retention",
    "retention_sql.PURGE_ASKS_BEFORE": "write; retention",
    "retention_sql.PURGE_DECISIONS_BEFORE": "write; retention",
    "retention_sql.PURGE_FETCH_LOG_BEFORE": "write; retention",
    "retention_sql.DELETE_EMPTY_WINDOWS": "write; removes windows left with no messages",
    "retention_sql.RECORD_OPT_OUT": "write; records an exclusion",
    "retention_sql.CLEAR_OPT_OUT": "write; removes an exclusion",
    "retention_sql.IS_OPTED_OUT": "existence check on the exclusion list, no content",
    "retention_sql.OPTED_OUT_PEOPLE": (
        "operator report; who has opted out and when, no message or document text"
    ),
    "retention_sql.PURGE_PERSON_WINDOWS": (
        "write; opt-out, returns channel ids and start times only"
    ),
    "retention_sql.PURGE_PERSON_MESSAGES": "write; opt-out",
    "retention_sql.PURGE_PERSON_ASKS": "write; opt-out",
    "retention_sql.PURGE_PERSON_DECISIONS": "write; opt-out",
    "retention_sql.PURGE_PERSON_FETCHES": "write; opt-out, the fetch log of their messages",
    "retention_sql.PURGE_PERSON_REACTIONS": "write; opt-out",
    "retention_sql.PURGE_PERSON_MENTIONS": "write; opt-out",
    # --- admin_sql -------------------------------------------------------
    #
    # The admin console configures the agent and cannot reach the corpus.
    # There is no message, chunk or ask column in any statement below, so
    # there is no viewer to scope them to -- and giving the console one would
    # create the second path into private channels the capability exists to
    # not create. If a content-returning SELECT ever appears in `admin_sql`,
    # it lands here unregistered and this file fails.
    "admin_sql.INSERT_ADMIN_TOKEN": (
        "write; stores the hash of one operator's console credential. Returns "
        "no row, and no corpus table is named"
    ),
    "admin_sql.LOOKUP_ADMIN_TOKEN": (
        "authentication; exchanges a credential hash for the operator it names. "
        "This is what *establishes* an identity, so it cannot be scoped to one: "
        "it returns an operator name and nothing else, and no request field "
        "feeds it -- only the presented credential does"
    ),
    "admin_sql.REVOKE_OPERATOR_TOKENS": (
        "write; withdraws one operator's credentials, keyed on that operator so "
        "no one else's access is touched"
    ),
    "admin_sql.COUNT_LIVE_OPERATOR_TOKENS": (
        "a count of one operator's live credentials, so a grant or a withdrawal "
        "can be recorded with a before and an after. No content"
    ),
    "admin_sql.ACTIVE_ADMIN_TOKENS": (
        "operator review; credential hashes, operator names and labels. No "
        "plaintext credential is stored, so none can be returned, and no "
        "corpus table is named"
    ),
    "admin_sql.APPEND_CONFIG_AUDIT": (
        "write; the configuration change record. Append-only -- there is "
        "deliberately no UPDATE or DELETE counterpart in admin_sql, and "
        "migration 0012 adds a trigger refusing both"
    ),
    "admin_sql.RECENT_CONFIG_AUDIT": (
        "operator report; who changed which setting, from what to what, and "
        "when. Settings and their values, never message, document or ask "
        "content -- and `admin.audit` refuses to record a change naming an "
        "environment-only secret, so one cannot arrive through this column"
    ),
    "admin_sql.INSERT_ADMIN_LOGIN": (
        "write; one console sign-in in flight: hashes of state, nonce and "
        "browser binding, and the encrypted PKCE verifier. No corpus table"
    ),
    "admin_sql.FORGET_EXPIRED_ADMIN_LOGINS": "write; drops sign-ins that expired a day ago",
    "admin_sql.CONSUME_ADMIN_LOGIN": (
        "authentication; marks one sign-in used and returns its hashes and "
        "ciphertext, keyed on the state's hash. No content"
    ),
    "admin_sql.INSERT_ADMIN_SESSION": (
        "write; a console session: its id hash, the person's subject, email and "
        "roles, and encrypted tokens. No corpus table"
    ),
    "admin_sql.LIVE_ADMIN_SESSION": (
        "authentication; exchanges a session-id hash for the session it names. "
        "This establishes an identity, so it cannot be scoped to one; it returns "
        "a subject, an email, roles and ciphertext, never corpus content"
    ),
    "admin_sql.REFRESH_ADMIN_SESSION": "write; replaces one session's tokens after a refresh",
    "admin_sql.TOUCH_ADMIN_SESSION": "write; marks one session seen",
    "admin_sql.REVOKE_ADMIN_SESSION": (
        "write; ends one session and returns it for sign-out. No content"
    ),
    # --- config_sql ------------------------------------------------------
    "config_sql.LOAD_SETTINGS": (
        "operator configuration; the settings an operator has stored and who "
        "stored them. No viewer, and none possible: this is read by every "
        "long-running process on a refresh cadence, on behalf of nobody. "
        "Holds no message, document or ask content, and resolves no secret -- "
        "the platform token, the model key and the database URL are read from "
        "the environment and a row stored under one of those names is "
        "reported and ignored"
    ),
    "config_sql.UPSERT_SETTING": (
        "write; stores one setting and, in the same statement, the record of "
        "who changed it and what it replaced. Returns no row"
    ),
    "config_sql.CLEAR_SETTING": (
        "write; removes a stored setting so the environment takes it back, "
        "recording what was removed. Returns no row"
    ),
    "config_sql.RECORD_REFUSAL": (
        "write; records a configuration change that was refused and why. "
        "Carries no before and no after: the likeliest refusal is a "
        "credential pasted into a settings field, and keeping the rejected "
        "text would store the secret the refusal exists to keep out"
    ),
    # --- memory_sql ------------------------------------------------------
    #
    # Only the two RECALL statements return remembered text, and both bind
    # :channel_ids. Nothing below returns a question, an answer or a summary.
    "memory_sql.INSERT_TURN": (
        "write; stores one of the asker's own turns with its provenance. "
        "Returns the new id only, which is how a row dropped by the opt-out "
        "trigger is told apart from a stored one"
    ),
    "memory_sql.LOCK_PERSON_MEMORY": "advisory lock; reads no table",
    "memory_sql.REPLACE_WITH_SUMMARY": (
        "write; replaces one person's older turns in one location with a "
        "summary, computing its covered channels from the rows it deletes "
        "rather than trusting the caller. Returns the new id only. Its CTEs "
        "read channel id arrays and timestamps, never question or answer text"
    ),
    "memory_sql.FORGET_TURNS_AT": (
        "write; /forget. Keyed on the requester's own platform identity, bound "
        "as a predicate, so it can delete no one else's history. Returns no row"
    ),
    "memory_sql.FORGET_SUMMARIES_AT": "write; /forget, as FORGET_TURNS_AT",
    "memory_sql.FORGET_TURNS_EVERYWHERE": "write; /forget everywhere, as FORGET_TURNS_AT",
    "memory_sql.FORGET_SUMMARIES_EVERYWHERE": (
        "write; /forget everywhere, as FORGET_TURNS_AT"
    ),
    "memory_sql.PURGE_TURNS_BEFORE": "write; retention",
    "memory_sql.PURGE_SUMMARIES_BEFORE": (
        "write; retention, keyed on the oldest content a summary carries"
    ),
    # --- facts_sql -------------------------------------------------------
    #
    # Personal facts are not channel content, so there is no readable set to
    # bind. The person predicate stands in for it: every read and delete is
    # keyed on the requester's own platform identity, and nothing returns a
    # fact for a person id supplied on its own.
    "facts_sql.UPSERT_FACT": (
        "write; stores one of the asker's own facts against the person id the "
        "adapter resolved from the asker's platform identity. Returns the id "
        "only, which is how a row dropped by the opt-out trigger is told apart"
    ),
    "facts_sql.ADD_WALLET": (
        "write; adds one of the asker's own wallets against the person id the "
        "adapter resolved, as UPSERT_FACT. Returns the id only"
    ),
    "facts_sql.FACTS_OF_REQUESTER": (
        "returns facts, but only the requester's own: the person is bound from "
        "the viewer's platform identity in the WHERE clause, so no argument "
        "can name another person, and an opted-out person reads nothing. "
        "Facts are not channel-scoped; where an email may be shown is decided "
        "by app.facts.visible_facts. Hard-deleted, so no deleted_at"
    ),
    "facts_sql.FORGET_FACT": (
        "write; deletes one kind of the requester's own facts, keyed on their "
        "platform identity. Returns no row"
    ),
    "facts_sql.FORGET_FACT_VALUE": (
        "write; deletes one value of one kind (a wallet among several) of the "
        "requester's own facts, keyed on their platform identity as "
        "FORGET_FACT. Returns no row"
    ),
    "facts_sql.FORGET_ALL_FACTS": (
        "write; /forget everywhere, keyed on the requester's own platform "
        "identity as FORGET_FACT. Returns no row"
    ),
    # --- media_sql -------------------------------------------------------
    #
    # The voice-question ledger: seconds per person and month, never content.
    # --- feature_requests_sql: the person's own suggestions ---------------
    "feature_requests_sql.LOCK_PERSON": (
        "row lock on the submitter's own person row, keyed on their person id, "
        "so the daily limit's count holds. Returns the id only"
    ),
    "feature_requests_sql.NAME_PLACEHOLDER_PERSON": (
        "write; replaces the account-id placeholder name of the submitter's own "
        "person row with their Discord name. Returns no row"
    ),
    "feature_requests_sql.EXISTING_BY_HASH": (
        "the id of the submitter's own earlier suggestion with the same "
        "normalised text, keyed on their person id. No content"
    ),
    "feature_requests_sql.INSERT_WITHIN_LIMIT": (
        "write; stores the submitter's own words against the person id the "
        "adapter resolved from their platform identity. Returns the id only"
    ),
    "feature_requests_sql.IS_OPTED_OUT": "a flag for the submitter's own person id",
    "feature_requests_sql.FOR_PERSON": (
        "returns suggestions, but only the requester's own: the person id is "
        "resolved from their platform identity and bound in the WHERE clause. "
        "A suggestion is the person's own words, never channel content"
    ),
    "feature_requests_sql.SET_NOTIFY": (
        "write; the requester's answer to 'tell you when its status changes?', "
        "keyed on their person id as well as the row id. Returns no row"
    ),
    # --- privacy_sql: /privacy, the asker's own rows ------------------------
    "privacy_sql.IS_OPTED_OUT": (
        "the asker's own opt-out flag, keyed on their person id"
    ),
    "privacy_sql.PLATFORMS": (
        "the platform names of the asker's own accounts, keyed on their person "
        "id"
    ),
    "privacy_sql.FACTS": (
        "returns fact values, but only the asker's own, keyed on the asker's "
        "own person id, resolved from the interaction's user; the guild view "
        "drops the values"
    ),
    "privacy_sql.MEMORY_COUNTS": (
        "counts of the asker's own remembered turns and summaries, keyed on the"
        " asker's own person id, resolved from the interaction's user. No "
        "content"
    ),
    "privacy_sql.RECENT_QUESTIONS": (
        "the asker's own questions to the assistant, keyed on the asker's own "
        "person id, resolved from the interaction's user; shown in their DM "
        "only"
    ),
    "privacy_sql.TASKS": (
        "the asker's own scheduled questions, keyed on the asker's own person "
        "id, resolved from the interaction's user"
    ),
    "privacy_sql.ALERTS": (
        "the asker's own alerts, keyed on the asker's own person id, resolved "
        "from the interaction's user"
    ),
    "privacy_sql.NOTIFICATIONS": (
        "the asker's own notification setting and queued count, keyed on the "
        "asker's own person id, resolved from the interaction's user. No "
        "content"
    ),
    "privacy_sql.VOICE_SECONDS": (
        "the asker's own voice seconds this month, keyed on the asker's own "
        "person id, resolved from the interaction's user. No content"
    ),
    "privacy_sql.SUGGESTIONS": (
        "the asker's own suggestions, keyed on the asker's own person id, "
        "resolved from the interaction's user; a suggestion is their own words"
    ),
    "privacy_sql.TOKENS": (
        "labels and dates of the asker's own live tokens, keyed on the asker's "
        "own person id, resolved from the interaction's user. Never the hash"
    ),
    "privacy_sql.TRACES": (
        "a count of traces of the asker's own questions, keyed on the asker's "
        "own person id, resolved from the interaction's user. No content"
    ),
    "media_sql.LOCK_LEDGER": "advisory lock; reads no table",
    "media_sql.PERSON_OF": (
        "identity lookup; the asker's own person id and whether they opted "
        "out, keyed on their platform identity. No content"
    ),
    "media_sql.CREATE_PERSON": "write; a person row for an asker never seen before",
    "media_sql.LINK_PERSON": "write; links that person row to the asker's account",
    "media_sql.MONTH_USAGE": (
        "accounting; two sums of seconds for one month -- the asker's and "
        "everyone's. Returns numbers, never who or what"
    ),
    "media_sql.CHARGE": "write; adds seconds to the asker's row for the month",
    # --- erasure_sql: /privacy -> delete everything, the asker's own rows ----
    "erasure_sql.OPEN_REQUEST": (
        "write; records the asker's own erasure request, keyed on their person "
        "id. Returns the request's ids, step and counts, never content"
    ),
    "erasure_sql.STOP_REIMPORT": "write; sets the asker's own erasure cut on their person row",
    "erasure_sql.RECORD_TRACES": "write; a count on the asker's own request",
    "erasure_sql.PURGE_DERIVED": (
        "write; purge_person_derived for the asker's own person id. Returns nothing"
    ),
    "erasure_sql.FOLD_VOICE": (
        "write; moves the asker's own voice seconds to the anonymous total. "
        "Returns a number of seconds"
    ),
    "erasure_sql.TOMBSTONE_PERSON": "write; clears the asker's own display name",
    "erasure_sql.RESET_PREFERENCES": "write; deletes the asker's own notification setting",
    "erasure_sql.ADVANCE": (
        "write; records a finished step on one request. Returns ids, step and counts"
    ),
    "erasure_sql.OPEN_REQUESTS": (
        "ingest maintenance; open requests with the person's id and one "
        "platform id, to resume. Numbers and ids only, never content"
    ),
    # --- tracing_notice_sql: the person's own notice record ---------------
    "tracing_notice_sql.CLAIM_NOTICE": (
        "write; a notice version and time on the asker's own person row, "
        "refused for an opted-out person. No content"
    ),
}

# Viewer-scoped statements that do not filter tombstones, and why. Kept
# separate from UNSCOPED so an exemption cannot be smuggled in by reusing a
# reason written for something else.
TOMBSTONE_EXEMPT: dict[str, str] = {
    "sql.LIST_CHANNELS": "channels are not tombstoned; they carry is_indexed",
    "memory_sql.RECALL_TURNS": (
        "conversation turns are never tombstoned: /forget, retention, opt-out "
        "and person deletion hard-delete them, so there is no deleted_at to test"
    ),
    "memory_sql.RECALL_SUMMARIES": "summaries are hard-deleted, as RECALL_TURNS",
    "notify_sql.SETTLE_UNREADABLE": (
        "returns no content: it settles the recipient's own queued rows for "
        "channels they may no longer read. A notification about a deleted "
        "message is settled here exactly as one about a live message is, and "
        "joining `message` to check would leave the row pending for ever"
    ),
}


def scoped(name: str) -> bool:
    return VIEWER_BIND in str(STATEMENTS[name])


def unaudited(statements: Mapping[str, TextClause], registry: Mapping[str, str]) -> list[str]:
    """Statements that neither bind the viewer nor carry a written reason."""
    return sorted(
        name
        for name, statement in statements.items()
        if VIEWER_BIND not in str(statement) and not registry.get(name, "").strip()
    )


def test_the_modules_were_actually_scanned() -> None:
    """A reflection bug that finds nothing would make every test below pass."""
    assert len(STATEMENTS) > 50
    for module_name in SQL_MODULES:
        assert any(name.startswith(f"{module_name}.") for name in STATEMENTS)


def test_every_statement_binds_the_viewer_or_says_why_it_does_not() -> None:
    missing = unaudited(STATEMENTS, UNSCOPED)
    assert missing == [], (
        "these statements neither bind the viewer's channel set nor carry a "
        "reason in UNSCOPED: " + ", ".join(missing)
    )


def test_the_audit_fails_on_an_unregistered_content_statement() -> None:
    """The regression itself: a new content SELECT added outside the registry.

    The previous audit could not fail this way, because a statement it had not
    been told about was a statement it did not look at.
    """
    added = {
        **STATEMENTS,
        "sql.RECENT_MESSAGES": text("SELECT content FROM message ORDER BY created_at"),
    }
    assert unaudited(added, UNSCOPED) == ["sql.RECENT_MESSAGES"]


def test_a_blank_reason_does_not_count_as_a_registration() -> None:
    """Otherwise the registry becomes a list of names, which is what it replaced."""
    assert unaudited(STATEMENTS, {**UNSCOPED, "sql.UPSERT_MESSAGE": "   "}) == [
        "sql.UPSERT_MESSAGE"
    ]


@pytest.mark.parametrize("name", sorted(UNSCOPED))
def test_no_stale_registrations(name: str) -> None:
    """A reason left behind for a deleted statement is a reason nobody re-read."""
    assert name in STATEMENTS, f"{name} is registered in UNSCOPED but no longer exists"


@pytest.mark.parametrize("name", sorted(TOMBSTONE_EXEMPT))
def test_no_stale_tombstone_exemptions(name: str) -> None:
    assert name in STATEMENTS, f"{name} is exempt from the tombstone check but does not exist"


def test_a_statement_is_never_both_scoped_and_registered() -> None:
    """A registration on a scoped statement is a reason that stopped being true."""
    contradictions = sorted(n for n in UNSCOPED if n in STATEMENTS and scoped(n))
    assert contradictions == []


@pytest.mark.parametrize("name", sorted(n for n in STATEMENTS if scoped(n)))
def test_the_filter_is_in_the_same_statement_as_the_ranking(name: str) -> None:
    """Filtering after ranking silently under-returns on an approximate scan.

    Worst for the people in the fewest channels, which is the opposite of who
    a permission system should fail.
    """
    statement = str(STATEMENTS[name])
    if "ORDER BY" not in statement:
        pytest.skip("unranked statement")
    assert statement.index(VIEWER_BIND) < statement.index("ORDER BY"), (
        f"{name} binds the viewer's channels after its ranking"
    )


@pytest.mark.parametrize("name", sorted(n for n in STATEMENTS if scoped(n)))
def test_viewer_scoped_statements_exclude_tombstones(name: str) -> None:
    if name in TOMBSTONE_EXEMPT:
        pytest.skip(TOMBSTONE_EXEMPT[name])
    assert "deleted_at IS NULL" in str(STATEMENTS[name]), (
        f"{name} can return withdrawn content"
    )
