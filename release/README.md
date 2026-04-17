# wiz2obs_cli

This ZIP contains a single-file WizNote to Obsidian migration CLI.

## 1. Configure

Copy `config.example.env` to `.env` in this same folder, then edit at least:

```env
WIZ_TO_OBSIDIAN_OUTPUT_DIR=D:\your\obsidian\WizSync
WIZ_USER_ID=your_account
WIZ_PASSWORD=your_password
```

If your WizNote data is not in the default Windows location, also edit:

```env
WIZ_LEVELDB_DIR=%APPDATA%\WizNote\IndexedDB\http_wiznote-desktop_0.indexeddb.leveldb
WIZ_BLOB_DIR=%APPDATA%\WizNote\IndexedDB\http_wiznote-desktop_0.indexeddb.blob
WIZ_CACHE_DIR=%APPDATA%\WizNote\Cache
```

## 2. Run

Windows:

```powershell
.\wiz2obs_cli.exe sync
```

macOS / Linux:

```bash
chmod +x ./wiz2obs_cli
./wiz2obs_cli sync
```

## Common Commands

The examples below use Windows syntax. On macOS / Linux, replace `.\wiz2obs_cli.exe` with `./wiz2obs_cli`.

Preview without writing files:

```powershell
.\wiz2obs_cli.exe sync --dry-run
```

Override the output directory:

```powershell
.\wiz2obs_cli.exe sync --output D:\your\obsidian\WizSync
```

Force a full export:

```powershell
.\wiz2obs_cli.exe sync --full
```

Skip remote/cache hydration:

```powershell
.\wiz2obs_cli.exe sync --no-hydrate
```

Advanced commands are still available:

```powershell
.\wiz2obs_cli.exe export --help
.\wiz2obs_cli.exe scan --help
```
