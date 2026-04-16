from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


WIZ_PATH_ENV_KEYS = {
    "WIZ_TO_OBSIDIAN_OUTPUT_DIR",
    "WIZ_LEVELDB_DIR",
    "WIZ_BLOB_DIR",
    "WIZ_CACHE_DIR",
}


class ConfigTests(unittest.TestCase):
    def test_default_export_dir_uses_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            override = Path(temp_dir) / "vault" / "Wiz"
            with mock.patch.dict(os.environ, {"WIZ_TO_OBSIDIAN_OUTPUT_DIR": str(override)}):
                config = importlib.import_module("wiz_to_obsidian.config")
                importlib.reload(config)

                self.assertEqual(override, config.default_export_dir())

    def test_default_wiz_source_dirs_use_env_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            leveldb_dir = root / "wiz" / "leveldb"
            blob_dir = root / "wiz" / "blob"
            cache_dir = root / "wiz" / "cache"
            with mock.patch.dict(
                os.environ,
                {
                    "WIZ_LEVELDB_DIR": str(leveldb_dir),
                    "WIZ_BLOB_DIR": str(blob_dir),
                    "WIZ_CACHE_DIR": str(cache_dir),
                },
            ):
                config = importlib.import_module("wiz_to_obsidian.config")
                importlib.reload(config)

                self.assertEqual(leveldb_dir, config.default_leveldb_dir())
                self.assertEqual(blob_dir, config.default_blob_dir())
                self.assertEqual(cache_dir, config.default_cache_dir())

    def test_default_export_dir_falls_back_to_cwd_relative_path(self) -> None:
        config = importlib.import_module("wiz_to_obsidian.config")

        with tempfile.TemporaryDirectory() as temp_dir:
            cwd = Path.cwd()
            try:
                os.chdir(temp_dir)
                with mock.patch.dict(
                    os.environ,
                    {key: value for key, value in os.environ.items() if key not in WIZ_PATH_ENV_KEYS},
                    clear=True,
                ):
                    self.assertEqual(Path.cwd() / "wiz-export", config.default_export_dir())
            finally:
                os.chdir(cwd)
