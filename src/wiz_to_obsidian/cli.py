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
from .sync import IncrementalSyncPlan, incremental_sync_inventory, load_or_rebuild_sync_state, plan_incremental_sync, write_sync_state
from .wiz_cache import CachedWizClient, ChromiumCacheBackend
from .wiz_hydration import CompositeWizContentClient, HydrationResult, HydrationSourceTracker, hydrate_inventory
from .wiz_local import load_local_note_payloads, scan_local_wiz, scan_local_wiz_metadata, summarize_inventory
from .wiz_remote import RemoteWizConfig, RemoteWizClient

OUTPUT_ENV_VAR = "WIZ_TO_OBSIDIAN_OUTPUT_DIR"


def _extract_remote_client(client: object) -> RemoteWizClient | None:
    if isinstance(client, RemoteWizClient):
        return client
    from .wiz_hydration import CompositeWizContentClient

    if isinstance(client, CompositeWizContentClient):
        for c in client._clients:
            if isinstance(c, RemoteWizClient):
                return c
    return None


def _count_remote_updates(
    remote_versions: dict[str, dict],
    skipped_doc_guids: tuple[str, ...],
    sync_state,
) -> int:
    from datetime import datetime, timezone

    count = 0
    for doc_guid in skipped_doc_guids:
        remote_info = remote_versions.get(doc_guid)
        if not remote_info:
            continue
        data_modified = remote_info.get("dataModified")
        if not isinstance(data_modified, (int, float)):
            continue
        state_entry = sync_state.notes_by_doc_guid.get(doc_guid)
        if not state_entry or not state_entry.updated:
            continue
        try:
            remote_dt = datetime.fromtimestamp(data_modified / 1000, tz=timezone.utc)
            state_dt = datetime.fromisoformat(state_entry.updated)
            if state_dt.tzinfo is None:
                state_dt = state_dt.replace(tzinfo=timezone.utc)
        except (ValueError, OverflowError):
            continue
        if remote_dt > state_dt:
            count += 1
    return count


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


def _default_dotenv_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / ".env"
    return Path.cwd() / ".env"


def _load_dotenv(path: Path | None = None) -> None:
    if path is None:
        path = _default_dotenv_path()
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


def _add_wiz_source_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--leveldb-dir", type=Path, default=default_leveldb_dir())
    parser.add_argument("--blob-dir", type=Path, default=default_blob_dir())


def _add_hydration_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cache-dir", type=Path, default=default_cache_dir())
    parser.add_argument("--wiz-user-id", default=os.environ.get("WIZ_USER_ID"))
    parser.add_argument("--wiz-password", default=os.environ.get("WIZ_PASSWORD"))
    parser.add_argument("--wiz-token", default=os.environ.get("WIZ_TOKEN"))
    parser.add_argument("--wiz-auto-login-param", default=os.environ.get("WIZ_AUTO_LOGIN_PARAM"))
    parser.add_argument("--wiz-server-url", default=os.environ.get("WIZ_SERVER_URL", "https://as.wiz.cn"))
    parser.add_argument("--wiz-ks-url", default=os.environ.get("WIZ_KS_URL"))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wiz2obs_cli")

    subparsers = parser.add_subparsers(dest="command", required=True)
    scan_parser = subparsers.add_parser("scan")
    _add_wiz_source_args(scan_parser)

    export_parser = subparsers.add_parser("export")
    _add_wiz_source_args(export_parser)
    export_parser.add_argument("--output", type=Path, default=default_export_dir())
    export_parser.add_argument("--limit", type=int, default=None)
    export_parser.add_argument("--incremental", action="store_true")
    export_parser.add_argument("--hydrate-missing", action="store_true")
    _add_hydration_args(export_parser)

    sync_parser = subparsers.add_parser("sync")
    _add_wiz_source_args(sync_parser)
    sync_parser.add_argument("--output", type=Path, default=None)
    sync_parser.add_argument("--limit", type=int, default=None)
    sync_parser.add_argument("--full", action="store_true")
    sync_parser.add_argument("--no-hydrate", action="store_true")
    sync_parser.add_argument("--dry-run", action="store_true")
    _add_hydration_args(sync_parser)
    return parser


def _write_json(stdout: TextIO, payload: dict) -> None:
    stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
    stdout.write("\n")


def _write_progress(stderr: TextIO, stage: str, message: str) -> None:
    stderr.write(f"[{stage}] {message}\n")
    stderr.flush()


def _format_duration(seconds: float) -> str:
    return f"{seconds:.2f}s"


def _prepare_sync_args(args, *, stderr: TextIO) -> None:
    if args.output is None:
        env_output = os.environ.get(OUTPUT_ENV_VAR)
        if env_output:
            args.output = Path(env_output)
        else:
            stderr.write(
                "wiz2obs_cli sync: error: missing output directory: "
                f"pass --output <Obsidian export dir> or set {OUTPUT_ENV_VAR} in .env\n"
            )
            raise SystemExit(2)

    args.incremental = not args.full
    args.hydrate_missing = not args.no_hydrate


def _sync_dry_run_payload(args) -> dict[str, object]:
    return {
        "command": "sync",
        "dry_run": True,
        "mode": "full" if args.full else "incremental",
        "hydrate": not args.no_hydrate,
        "output_dir": str(args.output),
        "leveldb_dir": str(args.leveldb_dir),
        "blob_dir": str(args.blob_dir),
        "cache_dir": str(args.cache_dir),
        "limit": args.limit,
    }


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


def _select_inventory_doc_guids(inventory: Inventory, doc_guids: set[str]) -> Inventory:
    return Inventory(
        notes=tuple(note for note in inventory.notes if note.doc_guid in doc_guids),
        resource_bytes_by_key=inventory.resource_bytes_by_key,
        attachment_bytes_by_key=inventory.attachment_bytes_by_key,
    )


def _build_hydration_client(args) -> CompositeWizContentClient | CachedWizClient | RemoteWizClient:
    clients = []
    cache_auth = None
    cache_available = False

    cache_dir = getattr(args, "cache_dir", None)
    if cache_dir:
        try:
            cached_client = CachedWizClient(ChromiumCacheBackend(cache_dir))
        except RuntimeError:
            cached_client = None
        if cached_client is not None:
            clients.append(cached_client)
            cache_available = True
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
    composite = CompositeWizContentClient(clients)
    composite.source_tracker = HydrationSourceTracker(cache_available=cache_available)
    return composite


def main(
    argv: list[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    scan_inventory_fn=scan_local_wiz,
    scan_inventory_metadata_fn=scan_local_wiz_metadata,
    load_note_payloads_fn=load_local_note_payloads,
    hydrate_inventory_fn=hydrate_inventory,
    build_hydration_client_fn=_build_hydration_client,
    export_inventory_fn=export_inventory,
    incremental_sync_inventory_fn=incremental_sync_inventory,
    time_fn=time.perf_counter,
) -> int:
    total_started_at = time_fn()
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    _load_dotenv()
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "sync":
        _prepare_sync_args(args, stderr=stderr)
        if args.dry_run:
            _write_json(stdout, _sync_dry_run_payload(args))
            return 0

    if args.command == "scan":
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
        _write_progress(stderr, "done", f"total elapsed: {_format_duration(time_fn() - total_started_at)}")
        _write_json(stdout, summarize_inventory(inventory))
        return 0

    stage_timings: dict[str, str] = {}
    planning_summary: dict[str, object] | None = None
    hydration_result: HydrationResult | None = None
    if args.incremental:
        scan_started_at = time_fn()
        _write_progress(stderr, "scan", "reading local Wiz metadata")
        metadata_inventory = scan_inventory_metadata_fn(leveldb_dir=args.leveldb_dir, blob_dir=args.blob_dir)
        metadata_duration = _format_duration(time_fn() - scan_started_at)
        _write_progress(
            stderr,
            "scan",
            (
                f"found {len(metadata_inventory.notes)} notes, "
                f"{sum(len(note.attachments) for note in metadata_inventory.notes)} attachments"
            ),
        )
        _write_progress(stderr, "scan", f"done in {metadata_duration}")
        stage_timings["metadata_scan"] = metadata_duration

        state_started_at = time_fn()
        sync_state_load_result = load_or_rebuild_sync_state(args.output)
        state_duration = _format_duration(time_fn() - state_started_at)
        stage_timings["state_load_or_rebuild"] = state_duration

        planning_started_at = time_fn()
        remote_versions = None
        remote_max_version = 0
        remote_att_version = 0
        if args.hydrate_missing and sync_state_load_result.state.notes_by_doc_guid:
            hydration_client = build_hydration_client_fn(args)
            try:
                remote_client = _extract_remote_client(hydration_client)
                if remote_client is not None:
                    remote_check_started_at = time_fn()
                    kb_guids = {note.kb_guid for note in metadata_inventory.notes if note.kb_guid}
                    for kb_guid in kb_guids:
                        try:
                            kb_info = remote_client.fetch_kb_info(kb_guid)
                            remote_kb_doc_version = kb_info.get("doc_version", 0)
                            remote_kb_att_version = kb_info.get("att_version", 0)
                            remote_att_version = max(remote_att_version, int(remote_kb_att_version or 0))
                            if remote_kb_doc_version > sync_state_load_result.state.doc_version:
                                _write_progress(
                                    stderr,
                                    "remote_check",
                                    f"kb {kb_guid}: remote doc_version={remote_kb_doc_version}, local={sync_state_load_result.state.doc_version}, fetching changes",
                                )
                                versions = remote_client.fetch_remote_note_versions(
                                    kb_guid, since_version=sync_state_load_result.state.doc_version
                                )
                                if remote_versions is None:
                                    remote_versions = {}
                                remote_versions.update(versions)
                                if versions:
                                    remote_max_version = max(
                                        remote_max_version,
                                        max(v.get("version", 0) for v in versions.values()),
                                    )
                            else:
                                _write_progress(
                                    stderr,
                                    "remote_check",
                                    f"kb {kb_guid}: remote doc_version={remote_kb_doc_version} <= local={sync_state_load_result.state.doc_version}, no remote changes",
                                )
                            if remote_kb_att_version > sync_state_load_result.state.att_version:
                                _write_progress(
                                    stderr,
                                    "remote_check",
                                    f"kb {kb_guid}: remote att_version={remote_kb_att_version}, local={sync_state_load_result.state.att_version}, asset refresh enabled",
                                )
                        except Exception as exc:
                            _write_progress(stderr, "remote_check", f"kb {kb_guid}: check failed: {exc}")
                    remote_check_duration = _format_duration(time_fn() - remote_check_started_at)
                    stage_timings["remote_version_check"] = remote_check_duration
                    if remote_versions:
                        remote_update_count = _count_remote_updates(
                            remote_versions, (), sync_state_load_result.state
                        )
                        _write_progress(
                            stderr,
                            "remote_check",
                            f"done in {remote_check_duration}: {len(remote_versions)} changed notes, {remote_update_count} newer than local",
                        )
                    else:
                        _write_progress(stderr, "remote_check", f"done in {remote_check_duration}: no remote changes")
            finally:
                close = getattr(hydration_client, "close", None)
                if callable(close):
                    close()

        preplan = plan_incremental_sync(
            metadata_inventory, args.output,
            sync_state=sync_state_load_result.state,
            remote_versions=remote_versions,
            remote_att_version=remote_att_version,
        )
        if args.limit is not None and len(preplan.notes_to_export) > args.limit:
            limited_notes = preplan.notes_to_export[:args.limit]
            limited_guids = {n.doc_guid for n in limited_notes}
            limited_reasons = {g: preplan.reasons_by_doc_guid[g] for g in limited_guids if g in preplan.reasons_by_doc_guid}
            limited_paths = {g: preplan.note_relative_paths_by_doc_guid[g] for g in limited_guids if g in preplan.note_relative_paths_by_doc_guid}
            extra_skipped = tuple(n.doc_guid for n in preplan.notes_to_export[args.limit:])
            preplan = IncrementalSyncPlan(
                notes_to_export=limited_notes,
                note_relative_paths_by_doc_guid=limited_paths,
                skipped_doc_guids=preplan.skipped_doc_guids + extra_skipped,
                stale_paths_to_remove=preplan.stale_paths_to_remove,
                reasons_by_doc_guid=limited_reasons,
            )
        planning_duration = _format_duration(time_fn() - planning_started_at)
        planning_summary = {
            "state_source": sync_state_load_result.source,
            "planned_notes": len(preplan.notes_to_export),
            "skipped_notes": len(preplan.skipped_doc_guids),
            "stale_paths_to_remove": len(preplan.stale_paths_to_remove),
            "deleted_notes": len(preplan.deleted_doc_guids),
        }
        _write_progress(
            stderr,
            "sync",
            (
                f"planned {len(preplan.notes_to_export)} notes, "
                f"skipped {len(preplan.skipped_doc_guids)}, "
                f"stale_paths {len(preplan.stale_paths_to_remove)}, "
                f"deleted {len(preplan.deleted_doc_guids)}"
            ),
        )
        stage_timings["planning"] = planning_duration

        changed_doc_guids = {note.doc_guid for note in preplan.notes_to_export}
        inventory = Inventory(notes=())
        if changed_doc_guids:
            payload_started_at = time_fn()
            collab_guids: set[str] = set()
            if args.hydrate_missing:
                collab_guids = {
                    n.doc_guid for n in metadata_inventory.notes
                    if n.doc_guid in changed_doc_guids and n.note_type == "collaboration"
                }
            non_collab_guids = changed_doc_guids - collab_guids
            if collab_guids:
                _write_progress(stderr, "scan", f"skipping local payloads for {len(collab_guids)} collaboration notes (hydration will fill)")
            if non_collab_guids:
                _write_progress(stderr, "scan", f"loading local payloads for {len(non_collab_guids)} planned notes")
                payload_inventory = _call_with_supported_kwargs(
                    load_note_payloads_fn,
                    metadata_inventory=metadata_inventory,
                    doc_guids=non_collab_guids,
                    leveldb_dir=args.leveldb_dir,
                    blob_dir=args.blob_dir,
                )
                inventory = _select_inventory_doc_guids(payload_inventory, non_collab_guids)
            elif not collab_guids:
                # No hydration split — load all changed note payloads
                _write_progress(stderr, "scan", f"loading local payloads for {len(changed_doc_guids)} planned notes")
                payload_inventory = _call_with_supported_kwargs(
                    load_note_payloads_fn,
                    metadata_inventory=metadata_inventory,
                    doc_guids=changed_doc_guids,
                    leveldb_dir=args.leveldb_dir,
                    blob_dir=args.blob_dir,
                )
                inventory = _select_inventory_doc_guids(payload_inventory, changed_doc_guids)
            # Merge collaboration notes from metadata_inventory (with metadata, no payloads)
            if collab_guids:
                collab_notes = tuple(n for n in metadata_inventory.notes if n.doc_guid in collab_guids)
                existing_guids = {n.doc_guid for n in inventory.notes}
                all_notes = tuple(n for n in inventory.notes) + tuple(n for n in collab_notes if n.doc_guid not in existing_guids)
                inventory = Inventory(
                    notes=all_notes,
                    resource_bytes_by_key=inventory.resource_bytes_by_key,
                    attachment_bytes_by_key=inventory.attachment_bytes_by_key,
                )
            payload_duration = _format_duration(time_fn() - payload_started_at)
            _write_progress(stderr, "scan", f"payload load done in {payload_duration}")
            stage_timings["payload_load"] = payload_duration

        hydration_repair_status = None
        if args.hydrate_missing and changed_doc_guids:
            hydrate_started_at = time_fn()
            _write_progress(stderr, "hydrate", "filling missing bodies/resources/attachments")
            hydration_client = build_hydration_client_fn(args)
            try:
                refresh_doc_guids = {
                    doc_guid for doc_guid, reason in preplan.reasons_by_doc_guid.items()
                    if reason in ("updated", "remote_updated")
                }
                hydration_result = _call_with_supported_kwargs(
                    hydrate_inventory_fn,
                    inventory=inventory,
                    client=hydration_client,
                    progress=lambda message: _write_progress(stderr, "hydrate", message),
                    refresh_note_bodies_for_doc_guids=refresh_doc_guids,
                )
                inventory = hydration_result.inventory
                hydration_repair_status = getattr(hydration_result, "note_repair_status", None)
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
                if getattr(hydration_result, "cache_unavailable", False):
                    _write_progress(stderr, "hydrate", "WARNING: local cache unavailable (WizNote may be running); degraded to remote-only")
            finally:
                close = getattr(hydration_client, "close", None)
                if callable(close):
                    close()
            hydrate_duration = _format_duration(time_fn() - hydrate_started_at)
            _write_progress(stderr, "hydrate", f"done in {hydrate_duration}")
            stage_timings["hydrate"] = hydrate_duration

        sync_started_at = time_fn()
        _write_progress(stderr, "sync", "running incremental sync")
        result = _call_with_supported_kwargs(
            incremental_sync_inventory_fn,
            inventory=inventory,
            output_dir=args.output,
            limit=args.limit,
            progress=lambda message: _write_progress(stderr, "sync", message),
            plan=preplan,
            sync_state=sync_state_load_result.state,
            hydration_repair_status=hydration_repair_status,
            doc_version=remote_max_version,
            att_version=remote_att_version,
        )
        sync_duration = _format_duration(time_fn() - sync_started_at)
        _write_progress(stderr, "sync", f"done in {sync_duration}")
        stage_timings["sync"] = sync_duration
    else:
        scan_started_at = time_fn()
        _write_progress(stderr, "scan", "reading local Wiz data")
        inventory = scan_inventory_fn(leveldb_dir=args.leveldb_dir, blob_dir=args.blob_dir)
        scan_duration = _format_duration(time_fn() - scan_started_at)
        _write_progress(
            stderr,
            "scan",
            (
                f"found {len(inventory.notes)} notes, "
                f"{sum(len(note.attachments) for note in inventory.notes)} attachments, "
                f"{inventory.resource_count} cached resources"
            ),
        )
        _write_progress(stderr, "scan", f"done in {scan_duration}")
        stage_timings["scan"] = scan_duration

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
                if getattr(hydration_result, "cache_unavailable", False):
                    _write_progress(stderr, "hydrate", "WARNING: local cache unavailable (WizNote may be running); degraded to remote-only")
            finally:
                close = getattr(hydration_client, "close", None)
                if callable(close):
                    close()
            hydrate_duration = _format_duration(time_fn() - hydrate_started_at)
            _write_progress(stderr, "hydrate", f"done in {hydrate_duration}")
            stage_timings["hydrate"] = hydrate_duration

        export_started_at = time_fn()
        _write_progress(stderr, "export", "writing Markdown/resources/attachments")
        result = _call_with_supported_kwargs(
            export_inventory_fn,
            inventory=inventory,
            output_dir=args.output,
            limit=args.limit,
            progress=lambda message: _write_progress(stderr, "export", message),
        )
        export_duration = _format_duration(time_fn() - export_started_at)
        _write_progress(stderr, "export", f"done in {export_duration}")
        stage_timings["export"] = export_duration
        result_sync_state = getattr(result, "sync_state", None)
        if result_sync_state is not None:
            write_sync_state(args.output, result_sync_state)
    payload = dict(result.report)
    payload["output_dir"] = str(result.output_dir)
    payload["report_path"] = str(result.report_path)
    if hydration_result is not None:
        payload["hydration"] = dict(hydration_result.summary)
        if getattr(hydration_result, "hydration_source_summary", None) is not None:
            payload["hydration"]["source_breakdown"] = hydration_result.hydration_source_summary
        if getattr(hydration_result, "cache_unavailable", False):
            payload["hydration"]["cache_unavailable"] = True
    if planning_summary is not None:
        payload["planning"] = planning_summary
    if stage_timings:
        stage_timings["total"] = _format_duration(time_fn() - total_started_at)
        payload["timings"] = stage_timings
    result.report_path.parent.mkdir(parents=True, exist_ok=True)
    result.report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_progress(stderr, "done", f"report: {result.report_path}")
    _write_progress(stderr, "done", f"total elapsed: {_format_duration(time_fn() - total_started_at)}")
    _write_json(stdout, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
