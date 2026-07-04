from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .table_markdown import TableConversionMode, convert_html_tables_in_markdown


REPORT_PATH = Path("_wiz") / "rewrite-tables-report.json"


@dataclass(frozen=True)
class RewriteFileReport:
    path: str
    html_tables: int
    converted_tables: int
    skipped_tables: int
    skipped_reasons: dict[str, int] = field(default_factory=dict)
    changed: bool = False

    def to_report(self) -> dict[str, object]:
        return {
            "path": self.path,
            "html_tables": self.html_tables,
            "converted_tables": self.converted_tables,
            "skipped_tables": self.skipped_tables,
            "skipped_reasons": dict(self.skipped_reasons),
            "changed": self.changed,
        }


@dataclass(frozen=True)
class RewriteTablesResult:
    input_dir: Path
    output_dir: Path | None
    mode: TableConversionMode
    dry_run: bool
    markdown_files: int
    html_tables: int
    converted_tables: int
    skipped_tables: int
    changed_files: int
    skipped_reasons: dict[str, int]
    files: tuple[RewriteFileReport, ...]

    def to_report(self) -> dict[str, object]:
        return {
            "input_dir": str(self.input_dir),
            "output_dir": str(self.output_dir) if self.output_dir is not None else None,
            "mode": self.mode.value,
            "dry_run": self.dry_run,
            "summary": {
                "markdown_files": self.markdown_files,
                "html_tables": self.html_tables,
                "converted_tables": self.converted_tables,
                "skipped_tables": self.skipped_tables,
                "changed_files": self.changed_files,
                "skipped_reasons": dict(self.skipped_reasons),
            },
            "files": [file_report.to_report() for file_report in self.files],
        }


def rewrite_tables(
    input_dir: Path,
    *,
    output_dir: Path | None = None,
    write: bool = False,
    force: bool = False,
    mode: TableConversionMode | str = TableConversionMode.HYBRID,
) -> RewriteTablesResult:
    input_root = input_dir.resolve()
    if not input_root.is_dir():
        raise FileNotFoundError(f"Input directory does not exist: {input_dir}")
    if write and output_dir is not None:
        raise ValueError("--write and --output are mutually exclusive")

    conversion_mode = TableConversionMode(mode)
    dry_run = output_dir is None and not write
    target_root = input_root
    resolved_output: Path | None = None

    if output_dir is not None:
        resolved_output = output_dir.resolve()
        _prepare_output_copy(input_root=input_root, output_root=resolved_output, force=force)
        target_root = resolved_output

    file_reports = _rewrite_markdown_files(target_root=target_root, dry_run=dry_run, mode=conversion_mode)
    result = _build_result(
        input_root=input_root,
        output_root=resolved_output,
        mode=conversion_mode,
        dry_run=dry_run,
        file_reports=file_reports,
    )

    if not dry_run:
        _write_report(target_root / REPORT_PATH, result)
    return result


def _prepare_output_copy(*, input_root: Path, output_root: Path, force: bool) -> None:
    if input_root == output_root:
        raise ValueError("--output must be different from --input")
    if output_root.exists():
        if not force:
            raise FileExistsError(f"Output directory already exists: {output_root}")
        if output_root.anchor == str(output_root):
            raise ValueError("Refusing to overwrite filesystem root")
        shutil.rmtree(output_root)
    shutil.copytree(input_root, output_root)


def _rewrite_markdown_files(
    *,
    target_root: Path,
    dry_run: bool,
    mode: TableConversionMode,
) -> tuple[RewriteFileReport, ...]:
    reports: list[RewriteFileReport] = []
    for path in sorted(target_root.rglob("*.md")):
        relative_path = path.relative_to(target_root)
        if _is_ignored_markdown_path(relative_path):
            continue

        original = path.read_text(encoding="utf-8")
        converted, stats = convert_html_tables_in_markdown(original, mode=mode)
        changed = converted != original
        if changed and not dry_run:
            path.write_text(converted, encoding="utf-8")

        reports.append(
            RewriteFileReport(
                path=relative_path.as_posix(),
                html_tables=stats.html_tables,
                converted_tables=stats.converted_tables,
                skipped_tables=stats.skipped_tables,
                skipped_reasons=dict(stats.skipped_reasons),
                changed=changed,
            )
        )
    return tuple(reports)


def _is_ignored_markdown_path(relative_path: Path) -> bool:
    parts = set(relative_path.parts)
    return "_wiz" in parts or ".obsidian" in parts


def _build_result(
    *,
    input_root: Path,
    output_root: Path | None,
    mode: TableConversionMode,
    dry_run: bool,
    file_reports: tuple[RewriteFileReport, ...],
) -> RewriteTablesResult:
    skipped_reasons: dict[str, int] = {}
    for file_report in file_reports:
        for reason, count in file_report.skipped_reasons.items():
            skipped_reasons[reason] = skipped_reasons.get(reason, 0) + count

    return RewriteTablesResult(
        input_dir=input_root,
        output_dir=output_root,
        mode=mode,
        dry_run=dry_run,
        markdown_files=len(file_reports),
        html_tables=sum(report.html_tables for report in file_reports),
        converted_tables=sum(report.converted_tables for report in file_reports),
        skipped_tables=sum(report.skipped_tables for report in file_reports),
        changed_files=sum(1 for report in file_reports if report.changed),
        skipped_reasons=skipped_reasons,
        files=file_reports,
    )


def _write_report(path: Path, result: RewriteTablesResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_report(), ensure_ascii=False, indent=2), encoding="utf-8")


__all__ = ["RewriteFileReport", "RewriteTablesResult", "rewrite_tables"]
