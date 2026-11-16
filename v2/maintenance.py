"""Maintenance operations shared by the API and CLI."""

from __future__ import annotations


def delete_url(db, store, url: str) -> dict:
    """Remove one URL's tasks, versions, vectors and events (V1 Delete URL)."""

    from v2.urls import canonicalize

    try:
        url = canonicalize(url)
    except ValueError:
        pass

    def delete_version(version: str, collection: str) -> None:
        store.delete_version(version, collection=collection)

    return db.delete_url(url, delete_version)
