from __future__ import annotations

import os
from pathlib import Path


def _path_from_env(env_name: str) -> Path | None:
    value = os.environ.get(env_name)
    if not value:
        return None
    return Path(value).expanduser()


def default_cache_dir() -> Path:
    override = _path_from_env("WIZ_CACHE_DIR")
    if override is not None:
        return override
    appdata = Path.home() / "AppData" / "Roaming"
    return appdata / "WizNote" / "Cache"


def default_leveldb_dir() -> Path:
    override = _path_from_env("WIZ_LEVELDB_DIR")
    if override is not None:
        return override
    appdata = Path.home() / "AppData" / "Roaming"
    return appdata / "WizNote" / "IndexedDB" / "http_wiznote-desktop_0.indexeddb.leveldb"


def default_blob_dir() -> Path:
    override = _path_from_env("WIZ_BLOB_DIR")
    if override is not None:
        return override
    appdata = Path.home() / "AppData" / "Roaming"
    return appdata / "WizNote" / "IndexedDB" / "http_wiznote-desktop_0.indexeddb.blob"


def default_export_dir() -> Path:
    override = _path_from_env("WIZ_TO_OBSIDIAN_OUTPUT_DIR")
    if override is not None:
        return override
    return Path.cwd() / "wiz-export"


__all__ = ["default_blob_dir", "default_cache_dir", "default_export_dir", "default_leveldb_dir"]
