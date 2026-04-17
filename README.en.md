# 📒 Wiz To Obsidian

[中文 README](README.md)

Export local WizNote notes to Obsidian while preserving folder structure, note bodies, images, attachments, and basic metadata as much as possible. The project also supports one-command incremental sync for daily use.

## ✨ Features

- Scan local Wiz IndexedDB data and build a complete note inventory
- Support both regular HTML notes and collaboration documents
- Hydrate missing note bodies, images, and attachments from local cache or remote Wiz APIs
- Export notes as Markdown that Obsidian can open directly
- Store resources under `_wiz/resources/`
- Store attachments under `_wiz/attachments/`
- Support one-command incremental sync

## 📌 Status

- Currently targets Windows + WizNote Desktop local data
- Sync direction is one-way: `Wiz -> Obsidian`
- Unit tests run independently on a clean machine and do not require real local Wiz data
- The repository is licensed under `MIT`

## 🧰 Requirements

- Python 3.10+
- Windows
- `git` available in `PATH`, because `requirements.txt` includes a Git dependency
- Wiz credentials or local Wiz cache if you want hydration to fill missing content

## 🚀 Quick Start

### 1. Install dependencies

```powershell
python -m pip install -r requirements.txt
```

### 2. Prepare `.env`

It is recommended to keep a `.env` file in the repository root with valid Wiz credentials so missing note bodies, images, and attachments can be hydrated.

Copy the template first:

```powershell
Copy-Item .env.example .env
```

Common fields:

```env
WIZ_USER_ID=your_account
WIZ_PASSWORD=your_password
WIZ_TOKEN=your_token
WIZ_AUTO_LOGIN_PARAM=your_auto_login_param
WIZ_SERVER_URL=https://as.wiz.cn
WIZ_KS_URL=https://ks.wiz.cn
```

Optional path overrides:

```env
WIZ_TO_OBSIDIAN_OUTPUT_DIR=D:\your\obsidian\WizSync
WIZ_LEVELDB_DIR=%APPDATA%\WizNote\IndexedDB\http_wiznote-desktop_0.indexeddb.leveldb
WIZ_BLOB_DIR=%APPDATA%\WizNote\IndexedDB\http_wiznote-desktop_0.indexeddb.blob
WIZ_CACHE_DIR=%APPDATA%\WizNote\Cache
```

### 3. Sync to Obsidian

```powershell
python .\scripts\sync_wiz_to_obsidian.py --output D:\your\obsidian\WizSync
```

If `WIZ_TO_OBSIDIAN_OUTPUT_DIR` is already set in `.env`, you can run:

```powershell
python .\scripts\sync_wiz_to_obsidian.py
```

Default behavior:

- Run incremental sync
- Enable hydration automatically
- Read `WIZ_TO_OBSIDIAN_OUTPUT_DIR` when `--output` is omitted

### 4. Force a full export

```powershell
python .\scripts\sync_wiz_to_obsidian.py --output D:\your\obsidian\WizSync --full
```

### 5. Preview without writing files

```powershell
python .\scripts\sync_wiz_to_obsidian.py --output D:\your\obsidian\WizSync --dry-run
```

## 📂 Output Layout

```text
<your-output-dir>/
├─ Folder A/
├─ Folder B/
└─ _wiz/
   ├─ resources/
   ├─ attachments/
   ├─ report.json
   ├─ content_audit.json
   └─ content_audit.md
```

- Markdown files follow the Wiz folder hierarchy
- Images and embedded resources are written to `_wiz/resources/<doc_guid>/`
- Attachments are written to `_wiz/attachments/<doc_guid>/`
- Links inside note bodies are rewritten to relative paths that Obsidian can open directly

## 🔄 Daily Sync

Run the same command again whenever WizNote has new content:

```powershell
python .\scripts\sync_wiz_to_obsidian.py --output D:\your\obsidian\WizSync
```

Incremental sync currently handles:

- New notes
- Notes whose body update time changed
- Notes whose attachment metadata changed even if the body timestamp did not change
- Notes whose title or category changed their output path
- Notes marked for repair because the previous export was incomplete
- Asset refresh when Wiz remote `att_version` advances for notes that already exported resources or attachments

Incremental sync also removes:

- Notes deleted from Wiz
- Orphaned exported resources under `_wiz/resources/<doc_guid>/`
- Orphaned exported attachments under `_wiz/attachments/<doc_guid>/`

## Releases

Tagged GitHub releases publish one ZIP per platform, each containing a single-file binary plus `config.example.env`, a minimal `README.md`, and `LICENSE`.

- `wiz2obs_cli-<tag>_windows_x64.zip`
- `wiz2obs_cli-<tag>_macos_x64.zip`
- `wiz2obs_cli-<tag>_linux_x64.zip`

Recommended binary usage:

1. Unzip the package for your platform.
2. Copy `config.example.env` to `.env`, then fill in the output directory and Wiz credentials.
3. Run `sync`.

Windows:

```powershell
.\wiz2obs_cli.exe sync
```

macOS / Linux:

```bash
chmod +x ./wiz2obs_cli
./wiz2obs_cli sync
```

You can also override the output directory:

```powershell
.\wiz2obs_cli.exe sync --output D:\your\obsidian\WizSync
```

## 🧩 Project Structure

- `src/wiz_to_obsidian/`: core library code for scanning, rendering, hydration, export, and incremental sync
- `scripts/`: user-facing Python entry scripts
- `tests/`: unit tests for scanning, rendering, export, and sync behavior

## 🧪 Run Tests

```powershell
python -m unittest discover -s tests -v
```

## 📄 License

This project is released under the `MIT License`. See [LICENSE](LICENSE).
