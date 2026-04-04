from __future__ import annotations


def build_export_report(
    *,
    total_notes: int,
    exported_notes: int,
    missing_bodies: tuple[str, ...] | list[str],
    missing_resources: tuple[str, ...] | list[str],
    exported_resources: int,
    exported_attachments: int,
) -> dict:
    missing_bodies = tuple(missing_bodies)
    missing_resources = tuple(missing_resources)
    return {
        "summary": {
            "total_notes": total_notes,
            "exported_notes": exported_notes,
            "missing_body_count": len(missing_bodies),
            "missing_resource_count": len(missing_resources),
            "exported_resources": exported_resources,
            "exported_attachments": exported_attachments,
        },
        "missing_bodies": list(missing_bodies),
        "missing_resources": list(missing_resources),
    }
