"""
Image storage for the Content service — local volume for dev, per the spec's
"Multipart upload + object storage (S3 or local volume for dev)". Files land
under CONTENT_MEDIA_DIR/{account_id}/{content_id}/{random}{ext}; the
storage-RELATIVE path is what goes in content.image_asset_ref, so swapping
this module for an S3 client later changes no schema and no route logic.

A re-upload writes a fresh random filename (the old bytes stay until the
content item is deleted — cheap insurance against serving a half-written
file); delete_content_media removes the whole per-item directory.
"""
from __future__ import annotations

import os
import shutil
import uuid

MEDIA_DIR = os.getenv("CONTENT_MEDIA_DIR", "/data/media")


def save_image(account_id: str, content_id: str, ext: str, data: bytes) -> str:
    """Persist the bytes; returns the storage-relative asset ref."""
    rel = os.path.join(account_id, content_id, f"{uuid.uuid4().hex}{ext}")
    path = os.path.join(MEDIA_DIR, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return rel


def full_path(asset_ref: str) -> str:
    return os.path.join(MEDIA_DIR, asset_ref)


def delete_content_media(account_id: str, content_id: str) -> None:
    """Best-effort cleanup when a content item is deleted."""
    shutil.rmtree(os.path.join(MEDIA_DIR, account_id, content_id), ignore_errors=True)
