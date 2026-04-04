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
        with mock.patch.dict(os.environ, {"WIZ_TO_OBSIDIAN_OUTPUT_DIR": "D:\\vault\\Wiz"}):
            config = importlib.import_module("wiz_to_obsidian.config")
            importlib.reload(config)

            self.assertEqual(Path("D:/vault/Wiz"), config.default_export_dir())

    def test_default_wiz_source_dirs_use_env_overrides(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "WIZ_LEVELDB_DIR": "D:\\wiz\\leveldb",
                "WIZ_BLOB_DIR": "D:\\wiz\\blob",
                "WIZ_CACHE_DIR": "D:\\wiz\\cache",
            },
        ):
            config = importlib.import_module("wiz_to_obsidian.config")
            importlib.reload(config)

            self.assertEqual(Path("D:/wiz/leveldb"), config.default_leveldb_dir())
            self.assertEqual(Path("D:/wiz/blob"), config.default_blob_dir())
            self.assertEqual(Path("D:/wiz/cache"), config.default_cache_dir())

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
                    self.assertEqual(Path(temp_dir) / "wiz-export", config.default_export_dir())
            finally:
                os.chdir(cwd)
