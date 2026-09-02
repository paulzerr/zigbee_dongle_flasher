from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from toolkit_common import (
    COORDINATOR_BACKUP,
    DATA_DIR,
    RUNS_DIR,
    assert_master_data,
    assert_runtime_versions,
    backup_summary,
    compare_backups,
    download_coordinator_backup,
    new_run_dir,
    print_backup_comparison,
    restore_coordinator_backup,
    same_provisioned_network,
    select_serial_port,
    sha256_file,
    update_overview,
    utc_now,
    validate_coordinator_backup,
    write_json,
)


FRAME_COUNTER_HIGH_WATER = DATA_DIR / "frame_counter_high_water.json"
FRAME_COUNTER_HEADROOM = 1_000_000
MAX_FRAME_COUNTER = 0xFFFFFFFF


def backup_frame_counter(backup: dict[str, Any] | None) -> int | None:
    if backup is None:
        return None
    value = backup.get("network_key", {}).get("frame_counter")
    if isinstance(value, int) and 0 <= value <= MAX_FRAME_COUNTER:
        return value
    return None


def load_recorded_high_water(path: Path = FRAME_COUNTER_HIGH_WATER) -> int:
    if not path.exists():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    value = data.get("network_frame_counter")
    if not isinstance(value, int) or not 0 <= value <= MAX_FRAME_COUNTER:
        raise RuntimeError(f"Invalid frame-counter high-water file: {path}")
    return value


def highest_observed_frame_counter(
    target: dict[str, Any],
    before: dict[str, Any] | None,
) -> int:
    counters = [load_recorded_high_water()]
    counters.extend(
        value
        for value in (backup_frame_counter(target), backup_frame_counter(before))
        if value is not None
    )
    for path in RUNS_DIR.glob("*/*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        value = backup_frame_counter(data)
        if value is not None:
            counters.append(value)
    return max(counters)


def record_high_water(value: int, *, reason: str) -> None:
    current = load_recorded_high_water()
    if value <= current:
        return
    write_json(
        FRAME_COUNTER_HIGH_WATER,
        {
            "network_frame_counter": value,
            "reason": reason,
            "updated_at": utc_now(),
        },
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safely clone the toolkit's coordinator state onto one Zigbee dongle."
    )
    parser.add_argument("--serial-port", default="auto", help="Serial port or 'auto'.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    assert_runtime_versions()
    assert_master_data()
    target = validate_coordinator_backup(COORDINATOR_BACKUP)
    serial_port = select_serial_port(args.serial_port)
    run_dir = new_run_dir("flash")
    before_path = run_dir / "before_restore.json"

    print(f"\nTarget dongle: {serial_port}")
    print(f"Restore data:  {COORDINATOR_BACKUP}")
    print(f"Restore SHA:   {sha256_file(COORDINATOR_BACKUP)}")
    print("\nCapturing the dongle's complete current coordinator state first...")
    backup_log_path = run_dir / "before_restore_command.json"
    backup_status_path = run_dir / "before_restore_status.json"
    try:
        download_coordinator_backup(serial_port, before_path, backup_log_path)
        before = validate_coordinator_backup(before_path)
        changes = compare_backups(before, target)
        backup_error = None
        write_json(
            backup_status_path,
            {
                "status": "available",
                "backup": str(before_path),
                "backup_sha256": sha256_file(before_path),
                "command_log": str(backup_log_path),
            },
        )
        print_backup_comparison(changes)
    except Exception as exc:
        before = None
        changes = []
        backup_error = str(exc)
        write_json(
            backup_status_path,
            {
                "status": "unavailable",
                "backup": None,
                "command_log": str(backup_log_path),
                "error": backup_error,
            },
        )
        print(f"\nWARNING: Could not back up this dongle: {backup_error}")
        print("No recovery backup is available. Continuing with the flash.")
        print(f"Backup status: {backup_status_path}")

    plan = {
        "operation": "restore_coordinator_backup",
        "serial_port": serial_port,
        "original_backup": str(before_path) if before is not None else None,
        "original_backup_sha256": sha256_file(before_path) if before is not None else None,
        "original_backup_status": "available" if before is not None else "unavailable",
        "original_backup_error": backup_error,
        "restore_source": str(COORDINATOR_BACKUP),
        "restore_source_sha256": sha256_file(COORDINATOR_BACKUP),
        "original_summary": backup_summary(before) if before is not None else None,
        "restore_summary": backup_summary(target),
        "changes": changes,
    }
    write_json(run_dir / "restore_plan.json", plan)
    original_backup_display = str(before_path) if before is not None else "none"
    print(f"\nOriginal dongle backup: {original_backup_display}")
    print(f"Restore plan:           {run_dir / 'restore_plan.json'}")

    highest_counter = highest_observed_frame_counter(target, before)
    before_counter = backup_frame_counter(before)
    if (
        before is not None
        and same_provisioned_network(before, target)
        and before_counter is not None
        and before_counter >= highest_counter
    ):
        record_high_water(before_counter, reason="observed on an already-current dongle")
        print("\nThis dongle already has the current network identity, keys, and device set.")
        print("Its counters are newer and will not be rolled back. Nothing was written.")
        update_overview("flasher_already_current", data_changed=False)
        return 0

    restore_counter = highest_counter + FRAME_COUNTER_HEADROOM
    if restore_counter > MAX_FRAME_COUNTER:
        raise RuntimeError(
            "Cannot reserve a safe Zigbee network frame counter; the 32-bit counter "
            "is too close to its maximum."
        )
    target_counter = backup_frame_counter(target)
    if target_counter is None:
        raise RuntimeError("Restore backup has an invalid network frame counter")
    counter_increment = restore_counter - target_counter
    plan["frame_counter_safety"] = {
        "highest_observed": highest_counter,
        "headroom": FRAME_COUNTER_HEADROOM,
        "restore_counter": restore_counter,
        "counter_increment": counter_increment,
        "high_water_file": str(FRAME_COUNTER_HIGH_WATER),
    }
    write_json(run_dir / "restore_plan.json", plan)
    record_high_water(restore_counter, reason=f"reserved for flash run {run_dir.name}")

    print("\nFrame-counter safety:")
    print(f"- highest observed: {highest_counter}")
    print(f"- restore counter:  {restore_counter}")
    print("\nThe restore will overwrite this dongle's coordinator identity and credentials.")
    print("Starting coordinator restore immediately...", flush=True)
    restore_coordinator_backup(
        serial_port,
        COORDINATOR_BACKUP,
        run_dir / "restore_command.json",
        counter_increment=counter_increment,
    )
    print("Coordinator restore command completed.", flush=True)
    update_overview("flashed_dongle", data_changed=False)
    print("\nRestore completed. Unplug and reconnect the dongle before validation.")
    print(f"Keep the recovery files in {run_dir}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
        raise SystemExit(130)
    except Exception as exc:
        print(f"\nStopped: {exc}")
        raise SystemExit(1)
