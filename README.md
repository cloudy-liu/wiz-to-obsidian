# 🧾 Wiz To Obsidian

[English README](README.en.md)

把本地 WizNote 笔记迁移到 Obsidian，尽量保留目录结构、正文、图片、附件和基础元数据，并支持后续一键增量同步。

## ✨ 功能概览

- 扫描本地 Wiz IndexedDB 数据并构建笔记清单
- 兼容普通 HTML 笔记与协作文档
- 从本地缓存或 Wiz 远端补全缺失的正文、图片和附件
- 导出为 Obsidian 可直接打开的 Markdown
- 图片和嵌入资源写入 `_wiz/resources/`
- 附件写入 `_wiz/attachments/`
- 支持一键增量同步

## 🚦 当前状态

- 当前主要面向 Windows + WizNote Desktop 本地数据目录
- 同步方向为单向：`Wiz -> Obsidian`
- 单元测试可在干净环境独立运行，不依赖真实 Wiz 数据
- 仓库采用 `MIT` 许可证

## ⚙️ 环境要求

- Python 3.10+
- Windows
- `git` 在 `PATH` 中可用，因为 `requirements.txt` 包含 Git 依赖
- 如需 hydration 补全，需要 Wiz 凭证或本地 Wiz 缓存

## 🚀 快速开始

### 📦 1. 安装依赖

```powershell
python -m pip install -r requirements.txt
```

### 🔐 2. 准备 `.env`

建议在仓库根目录放置 `.env`，至少提供一组 Wiz 凭证，用于补全本地缓存里缺失的正文、图片和附件。

先复制模板：

```powershell
Copy-Item .env.example .env
```

常见字段：

```env
WIZ_USER_ID=your_account
WIZ_PASSWORD=your_password
WIZ_TOKEN=your_token
WIZ_AUTO_LOGIN_PARAM=your_auto_login_param
WIZ_SERVER_URL=https://as.wiz.cn
WIZ_KS_URL=https://ks.wiz.cn
```

可选路径覆盖：

```env
WIZ_TO_OBSIDIAN_OUTPUT_DIR=D:\your\obsidian\WizSync
WIZ_LEVELDB_DIR=%APPDATA%\WizNote\IndexedDB\http_wiznote-desktop_0.indexeddb.leveldb
WIZ_BLOB_DIR=%APPDATA%\WizNote\IndexedDB\http_wiznote-desktop_0.indexeddb.blob
WIZ_CACHE_DIR=%APPDATA%\WizNote\Cache
```

### 🔄 3. 同步到 Obsidian

```powershell
python .\scripts\sync_wiz_to_obsidian.py --output D:\your\obsidian\WizSync
```

如果 `.env` 中已经设置了 `WIZ_TO_OBSIDIAN_OUTPUT_DIR`，也可以直接运行：

```powershell
python .\scripts\sync_wiz_to_obsidian.py
```

默认行为：

- 执行增量同步
- 自动开启 hydration
- 未传 `--output` 时读取 `WIZ_TO_OBSIDIAN_OUTPUT_DIR`

### 📚 4. 强制全量导出

```powershell
python .\scripts\sync_wiz_to_obsidian.py --output D:\your\obsidian\WizSync --full
```

### 👀 5. 仅预览，不实际写入

```powershell
python .\scripts\sync_wiz_to_obsidian.py --output D:\your\obsidian\WizSync --dry-run
```

## 🗂️ 输出结构

```text
<your-output-dir>/
├── Folder A/
├── Folder B/
└── _wiz/
   ├── resources/
   ├── attachments/
   ├── report.json
   ├── content_audit.json
   └── content_audit.md
```

- Markdown 按 Wiz 分类路径导出
- 图片和嵌入资源写入 `_wiz/resources/<doc_guid>/`
- 附件写入 `_wiz/attachments/<doc_guid>/`
- 正文中的资源链接会改写为 Obsidian 可直接打开的相对路径

## 🔁 日常同步

WizNote 有新内容后，重复执行同一条命令即可：

```powershell
python .\scripts\sync_wiz_to_obsidian.py --output D:\your\obsidian\WizSync
```

当前增量同步会处理：

- 新增笔记
- 正文更新时间变化的笔记
- 附件元数据变化的笔记，即使正文更新时间未变
- 标题或分类变化导致路径变化的笔记
- 上一次导出不完整、被标记为 `needs_repair` 的笔记
- Wiz 远端 `att_version` 前进后，已导出过资源或附件的笔记资产刷新

当前增量同步也会自动清理：

- 已从 Wiz 删除的笔记
- 对应 `_wiz/resources/<doc_guid>/` 下的旧资源
- 对应 `_wiz/attachments/<doc_guid>/` 下的旧附件
- 笔记仍存在但已不再需要的孤立资源和孤立附件

## 📦 Release 产物

GitHub tag release 会按平台发布 x64 ZIP 包，每个平台一个产物，包内包含单文件可执行程序、`config.example.env`、极简 `README.md` 和 `LICENSE`：

- `wiz2obs_cli-<tag>_windows_x64.zip`
- `wiz2obs_cli-<tag>_macos_x64.zip`
- `wiz2obs_cli-<tag>_linux_x64.zip`

下载后的推荐用法：

1. 解压对应平台 ZIP
2. 复制 `config.example.env` 为 `.env`，并填写输出目录和 Wiz 凭证
3. 运行 `sync`

Windows：

```powershell
.\wiz2obs_cli.exe sync
```

macOS / Linux：

```bash
chmod +x ./wiz2obs_cli
./wiz2obs_cli sync
```

也可以临时覆盖输出目录：

```powershell
.\wiz2obs_cli.exe sync --output D:\your\obsidian\WizSync
```

## 🛣️ 路径参数说明

- `--output`：目标导出目录，可以传 Obsidian vault 本身，也可以传 vault 内部子目录
- `--leveldb-dir`：Wiz 本地 leveldb 目录，通常为 `%APPDATA%\WizNote\IndexedDB\http_wiznote-desktop_0.indexeddb.leveldb`
- `--blob-dir`：Wiz 本地 blob 目录，通常为 `%APPDATA%\WizNote\IndexedDB\http_wiznote-desktop_0.indexeddb.blob`
- `--cache-dir`：Wiz Chromium cache 目录，通常为 `%APPDATA%\WizNote\Cache`，主要用于 hydration

如果 WizNote 使用标准 Windows 安装路径，这三个源目录参数通常无需显式传入；只有在数据目录被迁移、便携化，或当前机器路径与默认值不一致时才需要覆盖。

## 🧱 项目结构

- `src/wiz_to_obsidian/`：核心库代码，负责扫描、渲染、补全、导出和增量同步
- `scripts/`：用户直接执行的 Python 入口脚本
- `tests/`：覆盖扫描、渲染、导出与同步行为的单元测试

## ✅ 运行测试

```powershell
python -m unittest discover -s tests -v
```

## 📄 License

本项目基于 `MIT License` 发布，详见 [LICENSE](LICENSE)。
