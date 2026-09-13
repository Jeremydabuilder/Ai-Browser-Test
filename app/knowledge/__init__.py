"""Semantic History / Local RAG (Phase 13).

Lets PyBrowser search and reason over user-approved historical context -
browsing history, saved pages, Missions, findings, highlights, PDFs and
explicitly-added local files - entirely on-device. Off by default (see
``app.storage.settings.KEY_SEMANTIC_HISTORY_ENABLED``); nothing is indexed
until the user turns it on, and only content the user already chose to
keep (never a raw arbitrary page body, never form/password/clipboard
data) is ever indexed.

Embeddings are local and dependency-free (``embeddings.py``'s hashed
bag-of-words vectors) - nothing in this package makes a network call.
"""
