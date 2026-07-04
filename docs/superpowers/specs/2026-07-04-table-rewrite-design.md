# HTML Table Rewrite Design

## Context

Some WizNote exports contain readable but hard-to-edit HTML tables in the generated Obsidian Markdown. The existing Obsidian vault is the only usable source of truth because the Wiz account is no longer available, and the vault has already received manual edits after export. The first deliverable must therefore be a safe post-processing command for the existing vault. After that command is validated on a copy of the vault, the same conversion core should be reused inside the normal export and sync flow so future users get editable tables directly.

## Goals

- Convert safe HTML `<table>` blocks into Obsidian-friendly Markdown pipe tables.
- Preserve the current rendered table readability as much as possible.
- Preserve table images, links, inline code, emphasis, and line breaks.
- Avoid rewriting unrelated Markdown, frontmatter, resources, attachments, or Obsidian configuration.
- Make the first run safe by default: dry-run unless the user explicitly chooses an output copy or in-place write.
- Reuse one conversion implementation for both post-processing and future export-time conversion.

## Non-Goals

- Re-exporting from WizNote.
- Reformatting entire Markdown documents.
- Editing image/resource files under `_wiz`.
- Solving arbitrary HTML layout conversion.
- Converting tables that require `rowspan`, `colspan`, nested tables, or block-level code preservation in the first pass.

## Architecture

Add three focused modules/entry points:

- `src/wiz_to_obsidian/table_markdown.py`
  Owns HTML table parsing and Markdown table rendering. It exposes a pure conversion API that can be used by both post-processing and export.

- `src/wiz_to_obsidian/postprocess.py`
  Scans an existing exported vault, rewrites only convertible HTML table blocks, writes reports, and handles dry-run/output-copy/in-place modes.

- `src/wiz_to_obsidian/cli.py`
  Adds a `rewrite-tables` command for existing vaults. Later, adds `--table-mode` to `export` and `sync`.

The first implementation phase should only wire `rewrite-tables`; export/sync integration comes after validating the rewritten vault in Obsidian.

## Conversion Modes

Use one mode vocabulary across post-processing and export:

- `hybrid`
  Default. Convert tables when the structure is safe to express as Markdown; keep unsupported tables unchanged.

- `fidelity`
  Preserve HTML tables. This mode is useful once export integration exists, but it does not need much behavior in the first post-processing pass.

- `editable`
  Future extension. More aggressively flatten complex content into Markdown table cells.

The first phase should implement `hybrid` as the primary behavior and accept the mode option so the CLI contract can grow without breaking users.

## HTML Table Conversion Rules

Convertible structures:

- `<table>`, `<tr>`, `<th>`, `<td>`
- ordinary containers such as `<span>`, `<div>`, and `<p>`
- inline emphasis: `<strong>`, `<b>`, `<em>`, `<i>`
- inline code: `<code>`
- links: `<a href="...">text</a>`
- images: `<img src="..." alt="...">`
- line breaks: `<br>`

Default skip conditions in `hybrid` mode:

- any `rowspan` or `colspan`
- nested `<table>`
- `<pre>` blocks
- malformed tables that cannot be parsed into rows and cells
- empty tables

Cell rendering rules:

- Text is normalized without introducing real newlines inside table cells.
- `strong`/`b` becomes `**text**`.
- `em`/`i` becomes `*text*`.
- inline `code` becomes `` `code` ``.
- links become `[text](href)`.
- images become `![alt](src)` or `![](src)` when `alt` is empty.
- `<br>` becomes literal `<br>` inside the Markdown cell.
- pipe characters become `\|`.
- real newlines inside cells become `<br>`.

The converter must not rely on `markdownify` for table cells because `markdownify` can drop table images when `alt=""`.

## Post-Processing CLI

Add:

```bash
wiz2obs_cli rewrite-tables --input D:/notes/wiz-import-complete --dry-run --mode hybrid
```

Dry-run behavior:

- Scan Markdown files under `--input`.
- Do not modify files.
- Print a JSON summary to stdout.
- Report total Markdown files, HTML tables, converted table candidates, skipped tables, changed file candidates, and skip reasons.

Output-copy behavior:

```bash
wiz2obs_cli rewrite-tables --input D:/notes/wiz-import-complete --output D:/notes/wiz-import-complete-table-md --mode hybrid
```

- Copy the input vault to `--output`.
- Rewrite only the copy.
- Refuse to overwrite an existing output directory unless `--force` is passed.
- Preserve directory structure and non-Markdown files.

In-place behavior:

```bash
wiz2obs_cli rewrite-tables --input D:/notes/wiz-import-complete --write --mode hybrid
```

- Rewrite files in `--input`.
- Require explicit `--write`.
- Treat `--write` and `--output` as mutually exclusive.

Default behavior:

- If neither `--write` nor `--output` is supplied, behave as dry-run.

## Reporting

Write a report when changes are written:

```text
_wiz/rewrite-tables-report.json
```

The report should include:

- input directory
- output directory when applicable
- mode
- dry-run/write/output-copy mode
- Markdown file count
- HTML table count
- converted table count
- skipped table count
- changed file count
- per-file table counts
- skip reasons

The command should also print the same high-level summary to stdout as JSON.

## Existing Vault Safety

The post-processor must:

- only replace complete HTML `<table>...</table>` blocks that are successfully converted
- leave skipped tables byte-for-byte unchanged
- leave frontmatter unchanged unless a convertible table appears inside it, which should not happen in normal vaults
- leave existing Markdown pipe tables unchanged
- leave `_wiz/resources`, `_wiz/attachments`, `.obsidian`, and other non-Markdown files unchanged except for the report file

## Export Integration

After post-processing is validated:

- `render_note_markdown()` should accept a table mode.
- HTML-body conversion should protect and convert tables before generic `markdownify` conversion can drop image cells.
- `_render_table_block()` for collaboration notes should stop treating images, links, inline code, and `<br>` as reasons to force HTML table output.
- `export` and `sync` should accept `--table-mode hybrid|fidelity|editable`, defaulting to `hybrid` after validation.

## Test Plan

Add table conversion tests:

- basic HTML table to Markdown pipe table
- `th` header row handling
- image cells with empty alt text preserve `src`
- image cells with alt text preserve alt
- link conversion
- inline `strong`, `em`, and `code`
- `<br>` becomes cell-safe `<br>`
- pipe characters are escaped
- `rowspan`/`colspan` skipped in `hybrid`
- nested table skipped in `hybrid`
- `<pre>` skipped in `hybrid`
- multiple tables where convertible tables are replaced and skipped tables remain unchanged

Add post-processing tests:

- dry-run does not write files
- output-copy writes a modified copy and leaves input unchanged
- output-copy refuses existing destination without `--force`
- in-place write requires `--write`
- existing Markdown pipe tables are not modified
- report includes converted and skipped counts

Later export integration tests:

- HTML note body with table image exports to Markdown pipe table
- collaboration table with image/link/code cells exports to Markdown pipe table when structure is flat
- `export` and `sync` pass `--table-mode` through to rendering

## Validation Plan

Run the post-processor on a copy first:

```bash
wiz2obs_cli rewrite-tables --input D:/notes/wiz-import-complete --dry-run --mode hybrid
wiz2obs_cli rewrite-tables --input D:/notes/wiz-import-complete --output D:/notes/wiz-import-complete-table-md --mode hybrid
```

Open the copied vault in Obsidian and inspect representative files:

- the exported Tools note containing the VSCode settings table
- the exported JavaScript note containing language reference tables
- the exported HTML/CSS note containing tag reference tables
- the exported renovation/home note containing image-heavy tables
- the exported Perfetto note containing many technical reference tables

Acceptance criteria:

- converted tables remain readable in Reading View
- images still render
- links still work
- inline code remains legible
- Source Mode and Live Preview expose editable Markdown table rows
- Advanced Tables can operate on converted tables
- skipped tables remain unchanged

## Implementation Phases

1. Implement `table_markdown.py` and its unit tests.
2. Implement `postprocess.py` and `rewrite-tables` CLI tests.
3. Run `rewrite-tables` against a copied vault and validate in Obsidian.
4. Tune conversion rules or theme table image CSS only if validation shows a concrete issue.
5. Integrate the same converter into export/sync.
6. Update README with guidance for existing exports and future exports.
