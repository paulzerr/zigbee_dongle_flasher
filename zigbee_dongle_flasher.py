from __future__ import annotations

import argparse

from toolkit_common import (
    COORDINATOR_BACKUP,
    assert_master_data,
    backup_summary,
    compare_backups,
    download_coordinator_backup,
    new_run_dir,
    print_backup_comparison,
    require_confirmation,
    restore_coordinator_backup,
    same_provisioned_network,
    select_serial_port,
    sha256_file,
    update_overview,
    validate_coordinator_backup,
    write_json,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safely clone the toolkit's coordinator state onto one Zigbee dongle."
    )
    parser.add_argument("--serial-port", default="auto", help="Serial port or 'auto'.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    assert_master_data()
    target = validate_coordinator_backup(COORDINATOR_BACKUP)
    serial_port = select_serial_port(args.serial_port)
    run_dir = new_run_dir("flash")
    before_path = run_dir / "before_restore.json"

    print(f"\nTarget dongle: {serial_port}")
    print(f"Restore data:  {COORDINATOR_BACKUP}")
    print(f"Restore SHA:   {sha256_file(COORDINATOR_BACKUP)}")
    print("\nCapturing the dongle's complete current coordinator state first...")
    download_coordinator_backup(
        serial_port,
        before_path,
        run_dir / "before_restore_command.json",
    )
    before = validate_coordinator_backup(before_path)
    changes = compare_backups(before, target)
    print_backup_comparison(changes)

    plan = {
        "operation": "restore_coordinator_backup",
        "serial_port": serial_port,
        "original_backup": str(before_path),
        "original_backup_sha256": sha256_file(before_path),
        "restore_source": str(COORDINATOR_BACKUP),
        "restore_source_sha256": sha256_file(COORDINATOR_BACKUP),
        "original_summary": backup_summary(before),
        "restore_summary": backup_summary(target),
        "changes": changes,
    }
    write_json(run_dir / "restore_plan.json", plan)
    print(f"\nOriginal dongle backup: {before_path}")
    print(f"Restore plan:           {run_dir / 'restore_plan.json'}")

    if same_provisioned_network(before, target):
        print("\nThis dongle already has the current network identity, keys, and device set.")
        print("Its counters are newer and will not be rolled back. Nothing was written.")
        update_overview("flasher_already_current", data_changed=False)
        return 0

    print("\nThe restore will overwrite this dongle's coordinator identity and credentials.")
    require_confirmation(
        "RESTORE",
        "Review the changes above, then type RESTORE to continue: ",
    )
    restore_coordinator_backup(
        serial_port,
        COORDINATOR_BACKUP,
        run_dir / "restore_command.json",
    )
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
