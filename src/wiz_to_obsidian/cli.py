from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
import time
from pathlib import Path
from typing import TextIO

from .config import default_blob_dir, default_cache_dir, default_export_dir, default_leveldb_dir
from .exporter import export_inventory
from .models import Inventory
from .sync import incremental_sync_inventory, plan_incremental_sync
from .wiz_cache import CachedWizClient, ChromiumCacheBackend
from .wiz_hydration import CompositeWizContentClient, HydrationResult, hydrate_inventory
from .wiz_local import scan_local_wiz, summarize_inventory
from .wiz_remote import RemoteWizConfig, RemoteWizClient


DOTENV_KEY_ALIASES = {
    "user_id": "WIZ_USER_ID",
    "userid": "WIZ_USER_ID",
    "username": "WIZ_USER_ID",
    "email": "WIZ_USER_ID",
    "pw": "WIZ_PASSWORD",
    "password": "WIZ_PASSWORD",
    "token": "WIZ_TOKEN",
    "auto_login_param": "WIZ_AUTO_LOGIN_PARAM",
    "server_url": "WIZ_SERVER_URL",
    "account_server_url": "WIZ_SERVER_URL",
    "ks_url": "WIZ_KS_URL",
    "ks_server_url": "WIZ_KS_URL",
}


def _split_dotenv_assignment(line: str) -> tuple[str, str] | None:
    if "=" in line:
        key, value = line.split("=", 1)
        return key.strip(), value.strip()
    if ":" in line:
        key, value = line.split(":", 1)
        return key.strip(), value.strip()
    return None


def _load_dotenv(path: Path = Path(".env")) -> None:
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
        key = DOTENV_KEY_ALIASES.get(key.strip().lower(), key.strip())
        if not key or key in os.environ:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wiz-to-obsidian")

    subparsers = parser.add_subparsers(dest="command", required=True)
    scan_parser = subparsers.add_parser("scan")
    scan_parser.add_argument("--leveldb-dir", type=Path, default=default_leveldb_dir())
    scan_parser.add_argument("--blob-dir", type=Path, default=default_blob_dir())

    export_parser = subparsers.add_parser("export")
    export_parser.add_argument("--leveldb-dir", type=Path, default=default_leveldb_dir())
    export_parser.add_argument("--blob-dir", type=Path, default=default_blob_dir())
    export_parser.add_argument("--output", type=Path, default=default_export_dir())
    export_parser.add_argument("--limit", type=int, default=None)
    export_parser.add_argument("--incremental", action="store_true")
    export_parser.add_argument("--hydrate-missing", action="store_true")
    export_parser.add_argument("--cache-dir", type=Path, default=default_cache_dir())
    export_parser.add_argument("--wiz-user-id", default=os.environ.get("WIZ_USER_ID"))
    export_parser.add_argument("--wiz-password", default=os.environ.get("WIZ_PASSWORD"))
    export_parser.add_argument("--wiz-token", default=os.environ.get("WIZ_TOKEN"))
    export_parser.add_argument("--wiz-auto-login-param", default=os.environ.get("WIZ_AUTO_LOGIN_PARAM"))
    export_parser.add_argument("--wiz-server-url", default=os.environ.get("WIZ_SERVER_URL", "https://as.wiz.cn"))
    export_parser.add_argument("--wiz-ks-url", default=os.environ.get("WIZ_KS_URL"))
    return parser


def _write_json(stdout: TextIO, payload: dict) -> None:
    stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
    stdout.write("\n")


def _write_progress(stderr: TextIO, stage: str, message: str) -> None:
    stderr.write(f"[{stage}] {message}\n")
    stderr.flush()


def _format_duration(seconds: float) -> str:
    return f"{seconds:.2f}s"


def _call_with_supported_kwargs(function, /, **kwargs):
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return function(**kwargs)

    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()):
        return function(**kwargs)

    supported_kwargs = {key: value for key, value in kwargs.items() if key in signature.parameters}
    return function(**supported_kwargs)


def _merge_inventory(base_inventory: Inventory, refreshed_inventory: Inventory) -> Inventory:
    refreshed_by_doc_guid = {note.doc_guid: note for note in refreshed_inventory.notes}
    merged_notes = tuple(refreshed_by_doc_guid.get(note.doc_guid, note) for note in base_inventory.notes)

    merged_resources = dict(base_inventory.resource_bytes_by_key)
    merged_resources.update(refreshed_inventory.resource_bytes_by_key)

    merged_attachments = dict(base_inventory.attachment_bytes_by_key)
    merged_attachments.update(refreshed_inventory.attachment_bytes_by_key)

    return Inventory(
        notes=merged_notes,
        resource_bytes_by_key=merged_resources,
        attachment_bytes_by_key=merged_attachments,
    )


def _combine_hydration_summaries(*summaries: dict[str, int]) -> dict[str, int]:
    combined: dict[str, int] = {}
    for summary in summaries:
        for key, value in summary.items():
            combined[key] = combined.get(key, 0) + int(value)
    return combined


def _build_hydration_client(args) -> CompositeWizContentClient | CachedWizClient | RemoteWizClient:
    clients = []
    cache_auth = None

    cache_dir = getattr(args, "cache_dir", None)
    if cache_dir:
        try:
            cached_client = CachedWizClient(ChromiumCacheBackend(cache_dir))
        except RuntimeError:
            cached_client = None
        if cached_client is not None:
            clients.append(cached_client)
            if cached_client.cached_auth.token:
                cache_auth = cached_client.cached_auth

    explicit_token = getattr(args, "wiz_token", None)
    explicit_auto_login = getattr(args, "wiz_auto_login_param", None)
    explicit_password_auth = getattr(args, "wiz_user_id", None) and getattr(args, "wiz_password", None)
    if explicit_token or explicit_auto_login or explicit_password_auth:
        ks_server_url = getattr(args, "wiz_ks_url", None) or (cache_auth.ks_server_url if cache_auth is not None else None)
        token = explicit_token
        clients.append(
            RemoteWizClient(
                RemoteWizConfig(
                    account_server_url=args.wiz_server_url,
                    ks_server_url=ks_server_url,
                    user_id=args.wiz_user_id,
                    password=args.wiz_password,
                    token=token,
                    auto_login_param=args.wiz_auto_login_param,
                )
            )
        )

    if not clients:
        raise RuntimeError("No hydration source available. Configure Wiz credentials or close WizNote for cache access.")
    if len(clients) == 1:
        return clients[0]
    return CompositeWizContentClient(clients)


def main(
    argv: list[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    scan_inventory_fn=scan_local_wiz,
    hydrate_inventory_fn=hydrate_inventory,
    build_hydration_client_fn=_build_hydration_client,
    export_inventory_fn=export_inventory,
    incremental_sync_inventory_fn=incremental_sync_inventory,
    time_fn=time.perf_counter,
) -> int:
    total_started_at = time_fn()
    _load_dotenv()
    parser = _build_parser()
    args = parser.parse_args(argv)
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr

    scan_started_at = time_fn()
    _write_progress(stderr, "scan", "reading local Wiz data")
    inventory = scan_inventory_fn(leveldb_dir=args.leveldb_dir, blob_dir=args.blob_dir)
    _write_progress(
        stderr,
        "scan",
        (
            f"found {len(inventory.notes)} notes, "
            f"{sum(len(note.attachments) for note in inventory.notes)} attachments, "
            f"{inventory.resource_count} cached resources"
        ),
    )
    _write_progress(stderr, "scan", f"done in {_format_duration(time_fn() - scan_started_at)}")
    if args.command == "scan":
        _write_progress(stderr, "done", f"total elapsed: {_format_duration(time_fn() - total_started_at)}")
        _write_json(stdout, summarize_inventory(inventory))
        return 0

    hydration_result: HydrationResult | None = None
    if args.hydrate_missing:
        hydrate_started_at = time_fn()
        _write_progress(stderr, "hydrate", "filling missing bodies/resources/attachments")
        hydration_client = build_hydration_client_fn(args)
        try:
            hydration_result = _call_with_supported_kwargs(
                hydrate_inventory_fn,
                inventory=inventory,
                client=hydration_client,
                progress=lambda message: _write_progress(stderr, "hydrate", message),
            )
            inventory = hydration_result.inventory
            _write_progress(
                stderr,
                "hydrate",
                (
                    "done: "
                    f"notes+{hydration_result.summary.get('hydrated_notes', 0)}, "
                    f"resources+{hydration_result.summary.get('hydrated_resources', 0)}, "
                    f"attachments+{hydration_result.summary.get('hydrated_attachments', 0)}, "
                    f"failures={hydration_result.summary.get('hydration_failures', 0)}"
                ),
            )

            if args.incremental:
                preplan = plan_incremental_sync(inventory, args.output)
                if preplan.notes_to_export:
                    _write_progress(
                        stderr,
                        "hydrate",
                        f"refreshing {len(preplan.notes_to_export)} changed notes before export",
                    )
                    refresh_inventory = Inventory(
                        notes=preplan.notes_to_export,
                        resource_bytes_by_key=inventory.resource_bytes_by_key,
                        attachment_bytes_by_key=inventory.attachment_bytes_by_key,
                    )
                    refresh_result = _call_with_supported_kwargs(
                        hydrate_inventory_fn,
                        inventory=refresh_inventory,
                        client=hydration_client,
                        progress=lambda message: _write_progress(stderr, "hydrate", message),
                        refresh_note_bodies=True,
                    )
                    inventory = _merge_inventory(inventory, refresh_result.inventory)
                    hydration_result = HydrationResult(
                        inventory=inventory,
                        summary=_combine_hydration_summaries(
                            hydration_result.summary,
                            refresh_result.summary,
                        ),
                    )
                    _write_progress(
                        stderr,
                        "hydrate",
                        (
                            "refresh done: "
                            f"notes+{refresh_result.summary.get('hydrated_notes', 0)}, "
                            f"resources+{refresh_result.summary.get('hydrated_resources', 0)}, "
                            f"attachments+{refresh_result.summary.get('hydrated_attachments', 0)}, "
                            f"failures={refresh_result.summary.get('hydration_failures', 0)}"
                        ),
                    )
        finally:
            close = getattr(hydration_client, "close", None)
            if callable(close):
                close()
        _write_progress(stderr, "hydrate", f"done in {_format_duration(time_fn() - hydrate_started_at)}")

    if args.incremental:
        sync_started_at = time_fn()
        _write_progress(stderr, "sync", "planning incremental sync")
        result = _call_with_supported_kwargs(
            incremental_sync_inventory_fn,
            inventory=inventory,
            output_dir=args.output,
            limit=args.limit,
            progress=lambda message: _write_progress(stderr, "sync", message),
        )
        _write_progress(stderr, "sync", f"done in {_format_duration(time_fn() - sync_started_at)}")
    else:
        export_started_at = time_fn()
        _write_progress(stderr, "export", "writing Markdown/resources/attachments")
        result = _call_with_supported_kwargs(
            export_inventory_fn,
            inventory=inventory,
            output_dir=args.output,
            limit=args.limit,
            progress=lambda message: _write_progress(stderr, "export", message),
        )
        _write_progress(stderr, "export", f"done in {_format_duration(time_fn() - export_started_at)}")
    payload = dict(result.report)
    payload["output_dir"] = str(result.output_dir)
    payload["report_path"] = str(result.report_path)
    if hydration_result is not None:
        payload["hydration"] = dict(hydration_result.summary)
    _write_progress(stderr, "done", f"report: {result.report_path}")
    _write_progress(stderr, "done", f"total elapsed: {_format_duration(time_fn() - total_started_at)}")
    _write_json(stdout, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
