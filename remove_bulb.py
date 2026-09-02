from __future__ import annotations

import argparse
import asyncio
import sqlite3
import time
from typing import Any

import zigpy.config as zigpy_conf
from zigpy.device import Device
from zigpy_znp import commands as znp_c
from zigpy_znp.zigbee.application import ControllerApplication

from toolkit_common import (
    COORDINATOR_BACKUP,
    DATABASE,
    INVENTORY,
    atomic_copy,
    assert_master_data,
    assert_runtime_versions,
    backup_device_ids,
    download_coordinator_backup,
    load_inventory,
    new_run_dir,
    normalize_ieee,
    restore_coordinator_backup,
    select_serial_port,
    shutdown_zigpy_application,
    update_overview,
    validate_coordinator_backup,
    write_inventory,
    write_json,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Remove one bulb from the Zigbee network and toolkit data."
    )
    parser.add_argument("--label", help="Inventory label, for example BULB-L20.")
    parser.add_argument("--serial-port", default="auto", help="Serial port or 'auto'.")
    return parser.parse_args()


def requested_label(argument: str | None) -> str:
    label = argument.strip() if argument else input("Bulb label to remove: ").strip()
    if not label:
        raise RuntimeError("A bulb label is required")
    return label


def inventory_bulb(inventory: dict[str, Any], label: str) -> dict[str, Any]:
    for bulb in inventory["bulbs"]:
        if str(bulb.get("label", "")).casefold() == label.casefold():
            return bulb
    raise RuntimeError(f"Label {label!r} is not present in bulbs.json")


def build_config(serial_port: str) -> dict[str, Any]:
    return {
        zigpy_conf.CONF_DEVICE: {
            zigpy_conf.CONF_DEVICE_PATH: serial_port,
            zigpy_conf.CONF_DEVICE_BAUDRATE: 115200,
            zigpy_conf.CONF_DEVICE_FLOW_CONTROL: None,
        },
        zigpy_conf.CONF_DATABASE: str(DATABASE.resolve()),
        zigpy_conf.CONF_NWK_BACKUP_ENABLED: False,
        zigpy_conf.CONF_OTA: {zigpy_conf.CONF_OTA_ENABLED: False},
    }


def find_device(app: ControllerApplication, ieee: str) -> Device:
    wanted = normalize_ieee(ieee)
    for device in app.devices.values():
        if normalize_ieee(device.ieee) == wanted:
            return device
    raise RuntimeError(f"Bulb {ieee} is not present in zigpy.db")


def database_has_device(ieee: str) -> bool:
    wanted = normalize_ieee(ieee)
    uri = f"file:{DATABASE.resolve()}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        return any(
            normalize_ieee(row[0]) == wanted
            for row in connection.execute("SELECT ieee FROM devices_v13")
        )


def remove_backup_device(backup: dict[str, Any], ieee: str) -> dict[str, Any]:
    wanted = normalize_ieee(ieee)
    return {
        **backup,
        "devices": [
            device
            for device in backup["devices"]
            if normalize_ieee(device.get("ieee_address")) != wanted
        ],
    }


async def remove_from_network(serial_port: str, ieee: str) -> None:
    print("Connecting to the coordinator and loading zigpy.db...", flush=True)
    app = await ControllerApplication.new(build_config(serial_port))
    try:
        device = find_device(app, ieee)
        print("Sending the Zigbee leave command to the bulb...", flush=True)
        await app.remove(device.ieee, remove_children=False, rejoin=False)

        print("Waiting for the bulb to leave the network...", flush=True)
        deadline = asyncio.get_running_loop().time() + 35
        while device.ieee in app.devices:
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError("Timed out while removing the bulb from the network")
            await asyncio.sleep(0.25)
        print("Bulb left the network.", flush=True)

        print("Removing its coordinator security record...", flush=True)
        try:
            response = await app._znp.request(
                znp_c.ZDO.SecDeviceRemove.Req(IEEE=device.ieee),
            )
        except Exception as exc:
            print(
                f"Direct security-record removal was unavailable ({exc}); "
                "the verified cleanup fallback will handle it.",
                flush=True,
            )
        else:
            if int(response.Status) == 0:
                print("Coordinator security-record removal accepted.", flush=True)
            else:
                print(
                    f"Coordinator returned status {response.Status}; "
                    "the verified cleanup fallback will handle it.",
                    flush=True,
                )
    finally:
        print("Saving zigpy.db and closing the coordinator connection...", flush=True)
        await shutdown_zigpy_application(app)


def main() -> int:
    args = parse_args()
    assert_runtime_versions()
    inventory = load_inventory()
    label = requested_label(args.label)
    bulb = inventory_bulb(inventory, label)
    ieee = str(bulb["ieee"])
    assert_master_data(allow_missing_ieee=ieee)
    serial_port = select_serial_port(args.serial_port)
    run_dir = new_run_dir("remove")

    print(f"\nRemove: {bulb['label']} -> {ieee}")
    print("Power the bulb and keep it close to the known-good dongle.")
    print("This removes it from the network, zigpy.db, bulbs.json, and flasher data.")
    input("Press Enter to remove now: ")
    print("Starting removal...", flush=True)

    if database_has_device(ieee):
        asyncio.run(remove_from_network(serial_port, ieee))
    else:
        print("The bulb is already gone from zigpy.db; resuming the interrupted removal.")

    refreshed_path = run_dir / "coordinator_after_removal.json"
    print("\nRefreshing the coordinator backup used by the flasher...", flush=True)
    download_coordinator_backup(
        serial_port,
        refreshed_path,
        run_dir / "coordinator_after_removal_command.json",
    )
    refreshed = validate_coordinator_backup(refreshed_path)
    if normalize_ieee(ieee) in backup_device_ids(refreshed):
        cleaned_path = run_dir / "coordinator_without_bulb.json"
        write_json(cleaned_path, remove_backup_device(refreshed, ieee))
        print(
            "Z-Stack retained coordinator metadata after the leave; "
            "running the cleanup fallback...",
            flush=True,
        )
        restore_coordinator_backup(
            serial_port,
            cleaned_path,
            run_dir / "purge_coordinator_command.json",
        )
        print("Coordinator restarted; waiting 3 seconds before verification...", flush=True)
        time.sleep(3)
        verified_path = run_dir / "coordinator_after_purge.json"
        print("Verifying that the coordinator record is gone...", flush=True)
        download_coordinator_backup(
            serial_port,
            verified_path,
            run_dir / "coordinator_after_purge_command.json",
        )
        refreshed = validate_coordinator_backup(verified_path)
        if normalize_ieee(ieee) in backup_device_ids(refreshed):
            raise RuntimeError(
                "The coordinator still contains the bulb after the purge. "
                "Do not flash dongles; keep the run directory for recovery."
            )
        refreshed_path = verified_path

    print("Updating bulbs.json and the flasher backup...", flush=True)
    inventory["bulbs"] = [
        item
        for item in inventory["bulbs"]
        if normalize_ieee(item.get("ieee")) != normalize_ieee(ieee)
    ]
    write_inventory(inventory)
    atomic_copy(refreshed_path, COORDINATOR_BACKUP)
    update_overview(f"removed_{bulb['label']}", data_changed=True)

    print(f"\nRemoved {bulb['label']} -> {ieee}")
    print(f"Updated database:     {DATABASE}")
    print(f"Updated inventory:    {INVENTORY}")
    print(f"Updated flasher data: {COORDINATOR_BACKUP}")
    print(f"Command logs:         {run_dir}")
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
