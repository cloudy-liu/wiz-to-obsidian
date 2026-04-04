# 📒 Wiz To Obsidian

[English README](README.en.md)

把本地 WizNote 笔记迁移到 Obsidian，尽量保留目录结构、正文、图片、附件和基础元数据，并支持后续一键增量同步。

## ✨ 功能概览

- 扫描本地 Wiz IndexedDB，构建完整笔记清单
- 兼容普通 HTML 笔记和协作文档
- 自动补全本地缺失的正文、图片、附件
- 导出为 Obsidian 可直接打开的 Markdown
- 图片写入 `_wiz/resources/`
- 附件写入 `_wiz/attachments/`
- 支持一键日常增量同步

## 📌 项目状态

- 当前主要面向 Windows + WizNote Desktop 本地数据目录
- 同步方向是单向：`Wiz -> Obsidian`
- 单元测试默认可在陌生机器上独立运行，不要求本机真的有 Wiz 数据
- 当前仓库许可证为 `MIT`

## 🧰 环境要求

- Python 3.10+
- Windows
- 可访问 `git`，因为 `requirements.txt` 中包含 Git 依赖
- 如需 hydration 补全，准备好 Wiz 认证信息或本地 Wiz 缓存

## 🚀 最简单使用方式

### 1. 安装依赖

```powershell
python -m pip install -r requirements.txt
```

### 2. 准备 `.env`

推荐在仓库根目录放一个 `.env`，至少提供一套 Wiz 认证信息，用于补全本地缓存里没有的正文、图片和附件。

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

如需固定常用路径，也可以放进 `.env`：

```env
WIZ_TO_OBSIDIAN_OUTPUT_DIR=D:\your\obsidian\WizSync
WIZ_LEVELDB_DIR=%APPDATA%\WizNote\IndexedDB\http_wiznote-desktop_0.indexeddb.leveldb
WIZ_BLOB_DIR=%APPDATA%\WizNote\IndexedDB\http_wiznote-desktop_0.indexeddb.blob
WIZ_CACHE_DIR=%APPDATA%\WizNote\Cache
```

这些变量分别表示：

- `WIZ_TO_OBSIDIAN_OUTPUT_DIR`：导出到 Obsidian 的目标目录
- `WIZ_LEVELDB_DIR`：Wiz 本地 IndexedDB leveldb 目录
- `WIZ_BLOB_DIR`：Wiz 本地 IndexedDB blob 目录
- `WIZ_CACHE_DIR`：Wiz Chromium cache 目录，用于补全正文、图片和附件

### 3. 一键同步到 Obsidian

```powershell
python .\scripts\sync_wiz_to_obsidian.py --output D:\your\obsidian\WizSync
```

这里的 `--output` 应该传什么：

- 传一个 Obsidian 可见的目录
- 可以直接传整个独立 vault 目录
- 也可以传现有 vault 里的一个子目录
- 目录不存在时会自动创建

如果已经在 `.env` 中设置了 `WIZ_TO_OBSIDIAN_OUTPUT_DIR`，也可以直接运行：

```powershell
python .\scripts\sync_wiz_to_obsidian.py
```

脚本默认行为：

- 执行增量同步
- 自动做 hydration 补全
- 如未传 `--output`，则读取 `WIZ_TO_OBSIDIAN_OUTPUT_DIR`

### 4. 全量重跑

```powershell
python .\scripts\sync_wiz_to_obsidian.py --output D:\your\obsidian\WizSync --full
```

### 5. 先看计划，不实际写文件

```powershell
python .\scripts\sync_wiz_to_obsidian.py --output D:\your\obsidian\WizSync --dry-run
```

## 📂 导出结果

输出目录由你通过 `--output` 或 `WIZ_TO_OBSIDIAN_OUTPUT_DIR` 指定，例如：

```text
D:\your\obsidian\WizSync
```

典型结构：

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

说明：

- 正文 Markdown 按 Wiz 分类路径导出
- 图片、内嵌资源放 `_wiz/resources/<doc_guid>/`
- 附件放 `_wiz/attachments/<doc_guid>/`
- 正文里的链接会被改写为相对路径，Obsidian 可直接显示

## 🔄 日常同步

WizNote 有新内容后，重新执行同一条命令即可：

```powershell
python .\scripts\sync_wiz_to_obsidian.py --output D:\your\obsidian\WizSync
```

当前增量同步会处理：

- 新增笔记
- 正文更新时间变化的笔记
- 标题或分类变化导致路径变化的笔记

当前不会自动删除：

- 已经从 Wiz 中删除、但导出目录仍残留的旧文件

## 🛣️ 路径参数说明

- `--output`：目标导出目录。传 Obsidian vault 本身，或 vault 内部的某个子目录。
- `--leveldb-dir`：Wiz 本地 leveldb 目录。通常是 `%APPDATA%\WizNote\IndexedDB\http_wiznote-desktop_0.indexeddb.leveldb`。
- `--blob-dir`：Wiz 本地 blob 目录。通常是 `%APPDATA%\WizNote\IndexedDB\http_wiznote-desktop_0.indexeddb.blob`。
- `--cache-dir`：Wiz Chromium cache 目录。通常是 `%APPDATA%\WizNote\Cache`，主要在 hydration 阶段使用。

如果你的 WizNote 使用标准 Windows 安装路径，这三个 Wiz 源目录参数通常可以不传；只有当数据目录被迁走、做了便携化，或者当前机器路径和默认不一致时，才需要显式覆盖。

## 🧩 项目结构

- `src/wiz_to_obsidian/`：核心库代码，负责扫描、渲染、补全、导出、增量同步
- `scripts/`：用户直接执行的 Python 入口脚本
- `tests/`：覆盖扫描、渲染、导出、同步的单元测试

## 🧪 运行测试

clone 后装好依赖即可运行：

```powershell
python -m unittest discover -s tests -v
```

## 📄 License

本项目基于 `MIT License` 发布，详见 [LICENSE](LICENSE)。
