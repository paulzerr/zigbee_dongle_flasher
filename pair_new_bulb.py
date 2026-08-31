from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

import zigpy.config as zigpy_conf
from zigpy.device import Device
from zigpy.zcl.clusters.general import OnOff
from zigpy_znp.zigbee.application import ControllerApplication

from toolkit_common import (
    COORDINATOR_BACKUP,
    DATABASE,
    INVENTORY,
    atomic_copy,
    assert_master_data,
    backup_device_ids,
    backup_summary,
    compare_backups,
    download_coordinator_backup,
    load_inventory,
    new_run_dir,
    normalize_ieee,
    print_backup_comparison,
    require_confirmation,
    same_provisioned_network,
    select_serial_port,
    sha256_file,
    snapshot_master_data,
    update_overview,
    validate_coordinator_backup,
    write_inventory,
    write_json,
)


class PairListener:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[Device] = asyncio.Queue()

    def device_initialized(self, device: Device) -> None:
        if has_onoff_cluster(device):
            self.queue.put_nowait(device)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Guide one new light bulb onto the toolkit's Zigbee network."
    )
    parser.add_argument("--serial-port", default="auto", help="Serial port or 'auto'.")
    parser.add_argument("--label", help="Human-readable label, for example BULB-L20.")
    parser.add_argument("--permit-seconds", type=int, default=90)
    return parser.parse_args()


def find_input_cluster(device: Device, cluster_id: int) -> Any:
    if hasattr(device, "find_cluster"):
        return device.find_cluster(cluster_id)
    for endpoint in getattr(device, "endpoints", {}).values():
        cluster = getattr(endpoint, "in_clusters", {}).get(cluster_id)
        if cluster is not None:
            return cluster
    raise ValueError(f"Device {device.ieee} has no input cluster 0x{cluster_id:04x}")


def has_onoff_cluster(device: Device) -> bool:
    try:
        find_input_cluster(device, OnOff.cluster_id)
    except ValueError:
        return False
    return True


def describe_device(device: Device) -> str:
    return (
        f"ieee={device.ieee} nwk=0x{int(device.nwk):04x} "
        f"manufacturer={device.manufacturer!r} model={device.model!r}"
    )


def build_config(serial_port: str) -> dict[str, Any]:
    return {
        zigpy_conf.CONF_DEVICE: {
            zigpy_conf.CONF_DEVICE_PATH: serial_port,
            zigpy_conf.CONF_DEVICE_BAUDRATE: 115200,
            zigpy_conf.CONF_DEVICE_FLOW_CONTROL: None,
        },
        zigpy_conf.CONF_DATABASE: str(DATABASE.resolve()),
        zigpy_conf.CONF_OTA: {zigpy_conf.CONF_OTA_ENABLED: False},
    }


async def probe_onoff(device: Device, timeout: float = 10.0) -> bool:
    try:
        cluster = find_input_cluster(device, OnOff.cluster_id)
        await asyncio.wait_for(
            cluster.read_attributes(["on_off"], allow_cache=False),
            timeout,
        )
        return True
    except Exception:
        return False


async def pair_one(serial_port: str, permit_seconds: int) -> Device:
    app = await ControllerApplication.new(build_config(serial_port))
    listener = PairListener()
    app.add_listener(listener)
    known = {
        normalize_ieee(device.ieee)
        for device in app.devices.values()
        if device.ieee != app.state.node_info.ieee
    }

    try:
        print(f"\nPermit-join is open for {permit_seconds} seconds.")
        print("Factory-reset the new bulb now and keep other unpaired Zigbee devices off.")
        await app.permit(permit_seconds)
        deadline = asyncio.get_running_loop().time() + permit_seconds
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise RuntimeError("No new On/Off bulb joined before the permit window closed")
            try:
                candidate = await asyncio.wait_for(listener.queue.get(), remaining)
            except asyncio.TimeoutError as exc:
                raise RuntimeError(
                    "No new On/Off bulb joined before the permit window closed"
                ) from exc

            ieee = normalize_ieee(candidate.ieee)
            if ieee in known:
                print(f"Known device rejoined; ignoring {candidate.ieee}")
                continue
            if not await probe_onoff(candidate):
                print(f"New device did not answer an On/Off read; ignoring {candidate.ieee}")
                continue
            print(f"Paired: {describe_device(candidate)}")
            return candidate
    finally:
        app.remove_listener(listener)
        try:
            await app.permit(0)
        except Exception:
            pass
        await asyncio.sleep(1.5)
        await app.shutdown()


def requested_label(argument: str | None) -> str:
    label = argument.strip() if argument else input("New bulb label (for example BULB-L20): ").strip()
    if not label:
        raise RuntimeError("A bulb label is required")
    if any(character in label for character in (";", "\n", "\r")):
        raise RuntimeError("The label cannot contain semicolons or line breaks")
    return label


def main() -> int:
    args = parse_args()
    if args.permit_seconds < 10:
        raise RuntimeError("--permit-seconds must be at least 10")
    assert_master_data()
    inventory = load_inventory()
    label = requested_label(args.label)
    if any(str(item.get("label", "")).casefold() == label.casefold() for item in inventory["bulbs"]):
        raise RuntimeError(f"Label {label!r} already exists in bulbs.json")

    serial_port = select_serial_port(args.serial_port)
    run_dir = new_run_dir("pair")
    snapshot_master_data(run_dir / "before")
    dongle_before_path = run_dir / "dongle_before_pairing.json"
    print("\nCapturing the coordinator before opening permit-join...")
    download_coordinator_backup(
        serial_port,
        dongle_before_path,
        run_dir / "dongle_before_pairing_command.json",
    )
    dongle_before = validate_coordinator_backup(dongle_before_path)
    master_before = validate_coordinator_backup(COORDINATOR_BACKUP)
    comparison = compare_backups(dongle_before, master_before)
    print_backup_comparison(comparison)
    if not same_provisioned_network(dongle_before, master_before):
        raise RuntimeError(
            "This dongle does not match the toolkit's coordinator network. "
            "Use the flasher on the intended golden dongle before pairing."
        )

    plan = {
        "operation": "pair_new_bulb",
        "serial_port": serial_port,
        "label": label,
        "permit_seconds": args.permit_seconds,
        "database": str(DATABASE),
        "database_sha256_before": sha256_file(DATABASE),
        "inventory": str(INVENTORY),
        "inventory_sha256_before": sha256_file(INVENTORY),
        "coordinator_backup_before": str(dongle_before_path),
        "coordinator_backup_sha256_before": sha256_file(dongle_before_path),
        "coordinator_summary_before": backup_summary(dongle_before),
    }
    write_json(run_dir / "pairing_plan.json", plan)

    print("\nPairing will change the bulb, coordinator, zigpy.db, and bulbs.json.")
    print("The bulb factory reset cannot be undone by this script.")
    print(f"Complete before-snapshots are in {run_dir / 'before'}")
    require_confirmation("PAIR", "Type PAIR to open the join window: ")

    device = asyncio.run(pair_one(serial_port, args.permit_seconds))
    ieee = str(device.ieee).lower()
    if any(normalize_ieee(item.get("ieee")) == normalize_ieee(ieee) for item in inventory["bulbs"]):
        raise RuntimeError(f"The joined bulb {ieee} is already present in bulbs.json")
    inventory["bulbs"].append({"label": label, "ieee": ieee})
    write_inventory(inventory)
    update_overview("pair_pending_coordinator_refresh", data_changed=True)

    refreshed_path = run_dir / "coordinator_after_pairing.json"
    print("\nRefreshing the coordinator backup used by the flasher...")
    download_coordinator_backup(
        serial_port,
        refreshed_path,
        run_dir / "coordinator_after_pairing_command.json",
    )
    refreshed = validate_coordinator_backup(refreshed_path)
    if normalize_ieee(ieee) not in backup_device_ids(refreshed):
        raise RuntimeError(
            "Pairing succeeded, but the refreshed coordinator backup does not contain "
            f"the new bulb. Keep {run_dir} and do not flash other dongles yet."
        )
    atomic_copy(refreshed_path, COORDINATOR_BACKUP)
    update_overview("paired_new_bulb", data_changed=True)

    print(f"\nAdded {label} -> {ieee}")
    print(f"Updated database:           {DATABASE}")
    print(f"Updated inventory:          {INVENTORY}")
    print(f"Updated flasher data:       {COORDINATOR_BACKUP}")
    print(f"Snapshots and command logs: {run_dir}")
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
