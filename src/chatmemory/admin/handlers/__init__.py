"""The console's HTTP handlers, one module per thing an operator configures.

Every module here exports `routes(services)` and nothing else that the server
needs, so `server.py` assembles the application without knowing what any
individual screen does. Read them in this order:

    support     the shapes every handler shares: refusals, JSON, parsing
    queries     the read-only side, counts and timings only
    status      health, ingestion progress, embedding backlog
    settings    stored settings and their provenance
    federation  servers, their offered tools, and the allowlist
    channels    indexing scope, with what the corpus knows about readability
    optouts     per-person withdrawal
    tokens      MCP credentials, issued once and never recoverable
    changes     the change record, read back

Two rules hold in all of them, and neither is left to a reviewer to notice:

*   **Nothing returns corpus content.** No handler selects a message, a
    document or an ask body, and `queries` is the only module that reaches
    the corpus at all -- where the statements are counts and timings. A test
    reflects over them and fails on a statement that names a content column.
*   **Nothing returns a credential.** The one exception is the MCP token a
    handler has just minted, which is returned exactly once because it exists
    nowhere else; a stored credential is a hash and no route returns even
    that.
"""

from __future__ import annotations
