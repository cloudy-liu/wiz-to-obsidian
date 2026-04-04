from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


OUTPUT_ENV_VAR = "WIZ_TO_OBSIDIAN_OUTPUT_DIR"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_python(repo_root_path: Path) -> Path:
    venv_python = repo_root_path / ".venv" / "Scripts" / "python.exe"
    if venv_python.exists():
        return venv_python
    return Path(sys.executable)


def resolve_output_path(explicit_output: str | None) -> str | None:
    if explicit_output:
        return explicit_output
    env_output = os.environ.get(OUTPUT_ENV_VAR)
    if env_output:
        return env_output
    return None


def _split_dotenv_assignment(line: str) -> tuple[str, str] | None:
    if "=" in line:
        key, value = line.split("=", 1)
        return key.strip(), value.strip()
    if ":" in line:
        key, value = line.split(":", 1)
        return key.strip(), value.strip()
    return None


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()

        assignment = _split_dotenv_assignment(line)
        if assignment is None:
            continue

        key, value = assignment
        if not key or key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


def build_export_args(
    *,
    output: str,
    hydrate: bool,
    limit: int,
    leveldb_dir: str | None = None,
    blob_dir: str | None = None,
    cache_dir: str | None = None,
) -> list[str]:
    args = [
        "-m",
        "wiz_to_obsidian.cli",
        "export",
        "--output",
        output,
        "--incremental",
    ]
    if hydrate:
        args.append("--hydrate-missing")
    if leveldb_dir:
        args.extend(["--leveldb-dir", leveldb_dir])
    if blob_dir:
        args.extend(["--blob-dir", blob_dir])
    if cache_dir:
        args.extend(["--cache-dir", cache_dir])
    if limit > 0:
        args.extend(["--limit", str(limit)])
    return args


def build_env(repo_root_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str((repo_root_path / "src").resolve())
    return env


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync Wiz notes into the local Obsidian export directory.")
    parser.add_argument(
        "--output",
        help=(
            "Target export directory. Pass an Obsidian vault subdirectory such as "
            r"`D:\your\obsidian\WizSync`, or set WIZ_TO_OBSIDIAN_OUTPUT_DIR."
        ),
    )
    parser.add_argument("--full", action="store_true", help="Run a full export instead of incremental sync.")
    parser.add_argument("--no-hydrate", action="store_true", help="Skip hydration of missing bodies/resources/attachments.")
    parser.add_argument("--limit", type=int, default=0, help="Only export the first N notes. Useful for debugging.")
    parser.add_argument("--dry-run", action="store_true", help="Print the resolved command and exit without syncing.")
    parser.add_argument("--leveldb-dir", help="Override local Wiz IndexedDB leveldb directory.")
    parser.add_argument("--blob-dir", help="Override local Wiz IndexedDB blob directory.")
    parser.add_argument("--cache-dir", help="Override local Wiz cache directory used for hydration.")
    return parser


def _format_duration(seconds: float) -> str:
    return f"{seconds:.2f}s"


def main(argv: list[str] | None = None, *, time_fn=time.perf_counter) -> int:
    started_at = time_fn()
    parser = build_parser()
    args = parser.parse_args(argv)
    repo_root_path = repo_root()
    load_dotenv(repo_root_path / ".env")
    output = resolve_output_path(args.output)
    if output is None:
        parser.error(
            "missing output directory: pass --output <Obsidian export dir> "
            f"or set {OUTPUT_ENV_VAR}."
        )

    python = resolve_python(repo_root_path)
    hydrate = not args.no_hydrate
    export_args = build_export_args(
        output=output,
        hydrate=hydrate,
        limit=args.limit,
        leveldb_dir=args.leveldb_dir,
        blob_dir=args.blob_dir,
        cache_dir=args.cache_dir,
    )
    if args.full:
        export_args = [arg for arg in export_args if arg != "--incremental"]

    print(f"Repo   : {repo_root_path}")
    print(f"Output : {output}")
    print(f"Mode   : {'full' if args.full else 'incremental'}")
    print(f"Hydrate: {hydrate}")
    if args.leveldb_dir:
        print(f"LevelDB: {args.leveldb_dir}")
    if args.blob_dir:
        print(f"Blob   : {args.blob_dir}")
    if args.cache_dir:
        print(f"Cache  : {args.cache_dir}")
    if args.limit > 0:
        print(f"Limit  : {args.limit}")
    print(f'Command: "{python}" {" ".join(export_args)}')

    if args.dry_run:
        print(f"Elapsed: {_format_duration(time_fn() - started_at)}")
        return 0

    completed = subprocess.run(
        [str(python), *export_args],
        cwd=repo_root_path,
        env=build_env(repo_root_path),
        check=False,
    )
    print(f"Elapsed: {_format_duration(time_fn() - started_at)}")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
