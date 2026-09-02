from __future__ import annotations

import argparse

from toolkit_common import (
    COORDINATOR_BACKUP,
    atomic_copy,
    backup_summary,
    compare_backups,
    download_coordinator_backup,
    assert_runtime_versions,
    load_json,
    new_run_dir,
    print_backup_comparison,
    require_confirmation,
    select_serial_port,
    sha256_file,
    update_overview,
    validate_coordinator_backup,
    write_json,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download coordinator state from a Zigbee dongle into the toolkit."
    )
    parser.add_argument("--serial-port", default="auto", help="Serial port or 'auto'.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    assert_runtime_versions()
    serial_port = select_serial_port(args.serial_port)
    run_dir = new_run_dir("download")
    downloaded = run_dir / "downloaded_coordinator_backup.json"

    print(f"Reading coordinator state from {serial_port}")
    download_coordinator_backup(
        serial_port,
        downloaded,
        run_dir / "download_command.json",
    )
    incoming = validate_coordinator_backup(downloaded)

    comparison = []
    if COORDINATOR_BACKUP.exists():
        current_copy = run_dir / "previous_coordinator_backup.json"
        atomic_copy(COORDINATOR_BACKUP, current_copy)
        comparison = compare_backups(load_json(current_copy), incoming)
        print_backup_comparison(comparison)
    else:
        print("No existing coordinator backup is installed in data/.")

    plan = {
        "operation": "replace_master_coordinator_backup",
        "serial_port": serial_port,
        "created_backup": str(downloaded),
        "created_backup_sha256": sha256_file(downloaded),
        "installed_path": str(COORDINATOR_BACKUP),
        "previous_sha256": sha256_file(COORDINATOR_BACKUP) if COORDINATOR_BACKUP.exists() else None,
        "downloaded_summary": backup_summary(incoming),
        "changes": comparison,
    }
    write_json(run_dir / "download_plan.json", plan)

    print(f"\nDownloaded backup: {downloaded}")
    print(f"SHA-256: {plan['created_backup_sha256']}")
    require_confirmation(
        "SAVE",
        "Type SAVE to make this the coordinator backup used by the flasher: ",
    )

    print("Installing the downloaded coordinator backup...", flush=True)
    atomic_copy(downloaded, COORDINATOR_BACKUP)
    update_overview("download_from_dongle", data_changed=True)
    print(f"Installed current coordinator data at {COORDINATOR_BACKUP}")
    print(f"Previous and downloaded copies remain in {run_dir}")
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
