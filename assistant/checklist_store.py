"""Checklist template/cache loading and mapping helpers."""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from profile.identity import ProfileIdentityError, get_profile_identity

TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "config" / "checklist.json"
PROFILES_BASE_DIR = Path("data/profiles")

UI_TO_JSON_STATUS = {
    "not_started": "pending",
    "in_progress": "in_progress",
    "done": "completed",
}

JSON_TO_UI_STATUS = {
    "pending": "not_started",
    "in_progress": "in_progress",
    "completed": "done",
    "done": "done",
    "not_started": "not_started",
}

def _slug(value: str) -> str:
    lowered = value.strip().lower()
    lowered = re.sub(r"[^a-z0-9]+", "-", lowered)
    lowered = re.sub(r"-+", "-", lowered)
    return lowered.strip("-")


def _normalize_title(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _default_task(title: str, phase: str) -> Dict[str, Any]:
    return {
        "task_id": _slug(title),
        "title": title,
        "phase": phase,
        "priority": "normal",
        "due_offset_days": None,
        "due_date": "",
        "depends_on": [],
        "assigned_to": "",
        "status": "pending",
        "notes": "",
        "risk_flags": {
            "financial": False,
            "legal": False,
            "schedule": False,
        },
    }


def _read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Checklist JSON at {path} must be an object")
    return payload


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload.setdefault("metadata", {})
    payload["metadata"]["last_updated"] = datetime.now(timezone.utc).isoformat()
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def ensure_task_shape(payload: Dict[str, Any]) -> Dict[str, Any]:
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        tasks = []
    normalized_tasks: List[Dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        title = str(task.get("title", "")).strip()
        if not title:
            continue

        task.setdefault("task_id", _slug(title))
        task.setdefault("phase", str(task.get("phase", "")))
        task.setdefault("priority", "normal")
        task.setdefault("due_offset_days", None)
        task.setdefault("due_date", "")
        task.setdefault("depends_on", [])
        task.setdefault("assigned_to", "")
        task.setdefault("status", "pending")
        task.setdefault("notes", "")
        task.setdefault(
            "risk_flags",
            {"financial": False, "legal": False, "schedule": False},
        )
        normalized_tasks.append(task)

    payload["tasks"] = normalized_tasks
    return payload


def _merge_template_baseline(
    payload: Dict[str, Any],
    template_payload: Dict[str, Any],
) -> Dict[str, Any]:
    current_tasks = [task for task in payload.get("tasks", []) if isinstance(task, dict)]
    template_tasks = [task for task in template_payload.get("tasks", []) if isinstance(task, dict)]

    current_by_title = {
        _normalize_title(str(task.get("title", ""))): task
        for task in current_tasks
        if str(task.get("title", "")).strip()
    }

    for template_task in template_tasks:
        title = str(template_task.get("title", "")).strip()
        if not title:
            continue
        key = _normalize_title(title)
        if key not in current_by_title:
            cloned = copy.deepcopy(template_task)
            current_tasks.append(cloned)
            current_by_title[key] = cloned

    payload["tasks"] = current_tasks
    return payload


def load_template_payload() -> Dict[str, Any]:
    payload = _read_json(TEMPLATE_PATH)
    return ensure_task_shape(payload)


def save_template_payload(payload: Dict[str, Any]) -> None:
    _write_json(TEMPLATE_PATH, ensure_task_shape(payload))


def _profile_cache_path(owner_sub: str, house_slug: str) -> Path:
    return PROFILES_BASE_DIR / owner_sub / ".cache" / house_slug / "checklist.json"


def get_profile_cache_payload() -> Tuple[Dict[str, Any], Path | None]:
    template_payload = load_template_payload()
    try:
        owner_sub, house_slug = get_profile_identity()
    except ProfileIdentityError:
        return copy.deepcopy(template_payload), None

    cache_path = _profile_cache_path(owner_sub, house_slug)
    if not cache_path.exists():
        payload = copy.deepcopy(template_payload)
        _write_json(cache_path, payload)
        return payload, cache_path

    payload = ensure_task_shape(_read_json(cache_path))
    payload = _merge_template_baseline(payload, template_payload)
    payload = ensure_task_shape(payload)
    _write_json(cache_path, payload)
    return payload, cache_path


def save_profile_cache_payload(payload: Dict[str, Any], cache_path: Path | None) -> None:
    if cache_path is None:
        return
    template_payload = load_template_payload()
    normalized = ensure_task_shape(payload)
    normalized = _merge_template_baseline(normalized, template_payload)
    _write_json(cache_path, ensure_task_shape(normalized))


def payload_to_ui_rows(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for task in payload.get("tasks", []):
        if not isinstance(task, dict):
            continue
        title = str(task.get("title", "")).strip()
        if not title:
            continue
        rows.append(
            {
                "label": title,
                "status": JSON_TO_UI_STATUS.get(str(task.get("status", "pending")), "not_started"),
                "due_date": str(task.get("due_date", "") or "") or None,
                "category": str(task.get("phase", "")),
                "notes": str(task.get("notes", "")),
            }
        )
    return rows


def merge_ui_rows_into_payload(
    payload: Dict[str, Any],
    rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    updated = copy.deepcopy(payload)
    existing_tasks = [task for task in updated.get("tasks", []) if isinstance(task, dict)]
    by_normalized = {
        _normalize_title(str(task.get("title", ""))): task
        for task in existing_tasks
        if str(task.get("title", "")).strip()
    }

    for row in rows:
        label = str(row.get("label", "")).strip()
        if not label:
            continue
        norm = _normalize_title(label)
        task = by_normalized.get(norm)
        if not task:
            task = _default_task(label, str(row.get("category", "")))
            existing_tasks.append(task)
            by_normalized[norm] = task

        task["title"] = label
        task["task_id"] = task.get("task_id") or _slug(label)
        task["phase"] = str(row.get("category", ""))
        task["status"] = UI_TO_JSON_STATUS.get(str(row.get("status", "not_started")), "pending")
        due_date = row.get("due_date")
        task["due_date"] = "" if due_date in (None, "") else str(due_date)
        task["notes"] = str(row.get("notes", ""))

    updated["tasks"] = existing_tasks
    return ensure_task_shape(updated)
