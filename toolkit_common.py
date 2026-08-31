from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
RUNS_DIR = ROOT / "runs"
COORDINATOR_BACKUP = DATA_DIR / "coordinator_backup.json"
DATABASE = DATA_DIR / "zigpy.db"
INVENTORY = DATA_DIR / "bulbs.json"
OVERVIEW = ROOT / "overview.csv"

TRUSTED_WORDS = (
    "sonoff",
    "itead",
    "zigbee",
    "cc265",
    "cc26x",
)

OVERVIEW_FIELDS = (
    "data_last_changed_utc",
    "last_operation_utc",
    "last_operation",
    "bulb_count",
    "paired_bulbs",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def new_run_dir(operation: str) -> Path:
    path = RUNS_DIR / f"{run_stamp()}-{operation}"
    suffix = 1
    while path.exists():
        path = RUNS_DIR / f"{run_stamp()}-{operation}-{suffix}"
        suffix += 1
    path.mkdir(parents=True)
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()[:16]


def atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    shutil.copy2(source, temporary)
    os.replace(temporary, target)


def atomic_write_text(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, target)


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected a JSON object in {path}")
    return data


def validate_coordinator_backup(path: Path) -> dict[str, Any]:
    data = load_json(path)
    required = {
        "channel",
        "channel_mask",
        "coordinator_ieee",
        "devices",
        "extended_pan_id",
        "network_key",
        "pan_id",
        "security_level",
        "stack_specific",
    }
    missing = sorted(required - set(data))
    if missing:
        raise RuntimeError(f"Invalid coordinator backup {path}: missing {', '.join(missing)}")
    if not isinstance(data["devices"], list):
        raise RuntimeError(f"Invalid coordinator backup {path}: devices must be a list")
    network_key = data.get("network_key")
    if not isinstance(network_key, dict) or "key" not in network_key:
        raise RuntimeError(f"Invalid coordinator backup {path}: network key is missing")
    return data


def load_inventory(path: Path = INVENTORY) -> dict[str, Any]:
    data = load_json(path)
    bulbs = data.get("bulbs")
    if not isinstance(bulbs, list):
        raise RuntimeError(f"Invalid bulb inventory {path}")
    return data


def write_inventory(data: dict[str, Any], path: Path = INVENTORY) -> None:
    bulbs = data.get("bulbs", [])
    bulbs.sort(key=lambda item: str(item.get("label", "")))
    atomic_write_text(path, json.dumps({"bulbs": bulbs}, indent=2) + "\n")


def normalize_ieee(value: Any) -> str:
    return str(value).strip().lower().replace(":", "").replace("-", "")


def _device_map(backup: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        normalize_ieee(item.get("ieee_address")): item
        for item in backup.get("devices", [])
        if isinstance(item, dict) and item.get("ieee_address")
    }


def backup_device_ids(backup: dict[str, Any]) -> set[str]:
    return set(_device_map(backup))


def backup_summary(backup: dict[str, Any]) -> dict[str, Any]:
    devices = _device_map(backup)
    device_ids = sorted(devices)
    device_keys = [
        (ieee, devices[ieee].get("link_key", {}).get("key"))
        for ieee in device_ids
    ]
    network_key = backup.get("network_key", {})
    metadata = backup.get("metadata", {}).get("internal", {})
    return {
        "created_at": metadata.get("creation_time"),
        "channel": backup.get("channel"),
        "channel_mask": backup.get("channel_mask"),
        "pan_id": backup.get("pan_id"),
        "extended_pan_id": backup.get("extended_pan_id"),
        "coordinator_ieee": backup.get("coordinator_ieee"),
        "nwk_update_id": backup.get("nwk_update_id"),
        "security_level": backup.get("security_level"),
        "network_key_fingerprint": fingerprint(network_key.get("key")),
        "network_frame_counter": network_key.get("frame_counter"),
        "stack_specific_fingerprint": fingerprint(backup.get("stack_specific")),
        "device_count": len(device_ids),
        "device_ids_fingerprint": fingerprint(device_ids),
        "device_key_material_fingerprint": fingerprint(device_keys),
        "device_records_fingerprint": fingerprint(backup.get("devices")),
    }


def compare_backups(before: dict[str, Any], target: dict[str, Any]) -> list[dict[str, Any]]:
    old = backup_summary(before)
    new = backup_summary(target)
    fields = (
        "channel",
        "channel_mask",
        "pan_id",
        "extended_pan_id",
        "coordinator_ieee",
        "nwk_update_id",
        "security_level",
        "network_key_fingerprint",
        "network_frame_counter",
        "stack_specific_fingerprint",
        "device_count",
        "device_ids_fingerprint",
        "device_key_material_fingerprint",
        "device_records_fingerprint",
    )
    return [
        {
            "field": field,
            "before": old.get(field),
            "target": new.get(field),
            "changed": old.get(field) != new.get(field),
        }
        for field in fields
    ]


def same_provisioned_network(before: dict[str, Any], target: dict[str, Any]) -> bool:
    old = backup_summary(before)
    new = backup_summary(target)
    stable_fields = (
        "channel",
        "channel_mask",
        "pan_id",
        "extended_pan_id",
        "coordinator_ieee",
        "nwk_update_id",
        "security_level",
        "network_key_fingerprint",
        "stack_specific_fingerprint",
        "device_count",
        "device_ids_fingerprint",
        "device_key_material_fingerprint",
    )
    return all(old.get(field) == new.get(field) for field in stable_fields)


def print_backup_comparison(rows: list[dict[str, Any]]) -> None:
    print("\nChanges (current dongle -> restore target):")
    for row in rows:
        status = "CHANGE" if row["changed"] else "same"
        print(f"- {row['field']}: {row['before']!r} -> {row['target']!r} [{status}]")


def write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def list_serial_ports() -> list[dict[str, Any]]:
    try:
        from serial.tools import list_ports
    except ImportError as exc:
        raise RuntimeError("pyserial is not installed; run pip install -r requirements.txt") from exc

    ports: list[dict[str, Any]] = []
    for port in list_ports.comports():
        description = port.description or "no description"
        text = " ".join(
            str(value).lower()
            for value in (
                port.device,
                description,
                getattr(port, "manufacturer", None),
                getattr(port, "product", None),
            )
            if value
        )
        ports.append(
            {
                "device": port.device,
                "description": description,
                "trusted": any(word in text for word in TRUSTED_WORDS),
            }
        )
    return sorted(ports, key=lambda item: (not item["trusted"], item["device"]))


def select_serial_port(requested: str) -> str:
    if requested.strip().lower() != "auto":
        return requested.strip()

    ports = list_serial_ports()
    if not ports:
        raise RuntimeError("No serial devices were found")
    trusted = [item for item in ports if item["trusted"]]
    candidates = trusted or ports

    if len(candidates) == 1:
        item = candidates[0]
        print(f"Detected dongle: {item['device']} - {item['description']}")
        return str(item["device"])

    print("Available serial devices:")
    for index, item in enumerate(candidates, start=1):
        print(f"  {index}. {item['device']} - {item['description']}")
    while True:
        answer = input("Select a device number, or Q to quit: ").strip().lower()
        if answer == "q":
            raise KeyboardInterrupt
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            return str(candidates[int(answer) - 1]["device"])


def _run_tool(command: list[str], log_path: Path, mode: str) -> None:
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    write_json(
        log_path,
        {
            "created_at": utc_now(),
            "mode": mode,
            "command": command,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        },
    )
    if result.returncode != 0:
        raise RuntimeError(f"{mode} failed; see {log_path}")


def download_coordinator_backup(serial_port: str, output: Path, log_path: Path) -> None:
    if output.exists():
        raise RuntimeError(f"Refusing to overwrite backup capture: {output}")
    command = [
        sys.executable,
        "-m",
        "zigpy_znp.tools.network_backup",
        serial_port,
        "--output",
        str(output),
    ]
    _run_tool(command, log_path, "network_backup")
    validate_coordinator_backup(output)


def restore_coordinator_backup(serial_port: str, source: Path, log_path: Path) -> None:
    validate_coordinator_backup(source)
    command = [
        sys.executable,
        "-m",
        "zigpy_znp.tools.network_restore",
        serial_port,
        "--input",
        str(source),
    ]
    _run_tool(command, log_path, "network_restore")


def snapshot_sqlite(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"file:{source.resolve()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as src, sqlite3.connect(target) as dst:
        src.backup(dst)


def sqlite_user_version(path: Path = DATABASE) -> int:
    uri = f"file:{path.resolve()}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])


def snapshot_master_data(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    snapshot_sqlite(DATABASE, destination / "zigpy.db")
    atomic_copy(INVENTORY, destination / "bulbs.json")
    atomic_copy(COORDINATOR_BACKUP, destination / "coordinator_backup.json")


def update_overview(operation: str, *, data_changed: bool) -> None:
    inventory = load_inventory()
    names = sorted(str(item.get("label", "")).strip() for item in inventory["bulbs"])
    names = [name for name in names if name]
    now = utc_now()
    previous: dict[str, str] = {}
    if OVERVIEW.exists():
        with OVERVIEW.open("r", encoding="utf-8", newline="") as handle:
            previous = next(csv.DictReader(handle), {}) or {}
    previous_names = [name.strip() for name in previous.get("paired_bulbs", "").split(";") if name.strip()]
    if data_changed or names != previous_names or not previous.get("data_last_changed_utc"):
        data_last_changed = now
    else:
        data_last_changed = previous["data_last_changed_utc"]
    row = {
        "data_last_changed_utc": data_last_changed,
        "last_operation_utc": now,
        "last_operation": operation,
        "bulb_count": str(len(names)),
        "paired_bulbs": "; ".join(names),
    }
    temporary = OVERVIEW.with_name(f".{OVERVIEW.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OVERVIEW_FIELDS)
        writer.writeheader()
        writer.writerow(row)
    os.replace(temporary, OVERVIEW)


def require_confirmation(word: str, prompt: str) -> None:
    answer = input(prompt).strip()
    if answer != word:
        raise RuntimeError("Cancelled; no write operation was started")


def assert_master_data() -> None:
    for path in (COORDINATOR_BACKUP, DATABASE, INVENTORY):
        if not path.exists():
            raise RuntimeError(f"Missing required data file: {path}")
    backup = validate_coordinator_backup(COORDINATOR_BACKUP)
    inventory = load_inventory(INVENTORY)
    version = sqlite_user_version(DATABASE)
    if version != 13:
        raise RuntimeError(f"Expected zigpy database schema 13, found {version}")

    labels = [str(item.get("label", "")).strip() for item in inventory["bulbs"]]
    inventory_ids = [normalize_ieee(item.get("ieee")) for item in inventory["bulbs"]]
    if any(not label for label in labels) or any(not ieee for ieee in inventory_ids):
        raise RuntimeError("bulbs.json contains an empty label or IEEE address")
    if len(set(label.casefold() for label in labels)) != len(labels):
        raise RuntimeError("bulbs.json contains duplicate labels")
    if len(set(inventory_ids)) != len(inventory_ids):
        raise RuntimeError("bulbs.json contains duplicate IEEE addresses")

    uri = f"file:{DATABASE.resolve()}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        database_ids = {
            normalize_ieee(row[0])
            for row in connection.execute("SELECT ieee FROM devices_v13")
        }
    missing_from_db = sorted(set(inventory_ids) - database_ids)
    missing_from_backup = sorted(set(inventory_ids) - backup_device_ids(backup))
    if missing_from_db:
        raise RuntimeError(
            f"bulbs.json has {len(missing_from_db)} bulb(s) missing from zigpy.db"
        )
    if missing_from_backup:
        raise RuntimeError(
            "The coordinator backup is older than bulbs.json. Run "
            "download_from_dongle.py with the known-good dongle before flashing."
        )
