"""Sync storage providers - see base.py for the interface. Storage
transport is deliberately kept separate from encryption (app/sync/crypto.py):
a provider only ever sees opaque encrypted bytes, never plaintext.
"""
