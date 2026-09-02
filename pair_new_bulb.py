from __future__ import annotations

import argparse
import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Any

import zigpy.config as zigpy_conf
from zigpy.device import Device
from zigpy.zcl.clusters.general import OnOff
import zigpy_znp.types as znp_t
from zigpy_znp.api import ZNP
from zigpy_znp.zigbee.application import ControllerApplication
from zigpy_znp.znp import security as znp_security

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
    print("Connecting to the coordinator and loading zigpy.db...", flush=True)
    app = await ControllerApplication.new(build_config(serial_port))
    listener = PairListener()
    app.add_listener(listener)
    known = {
        normalize_ieee(device.ieee)
        for device in app.devices.values()
        if device.ieee != app.state.node_info.ieee
    }

    try:
        print(f"\nPermit-join is open for {permit_seconds} seconds.", flush=True)
        print("Factory-reset the new bulb now and keep other unpaired Zigbee devices off.")
        print("Waiting for a new bulb...", flush=True)
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
            print("New bulb detected; checking that it responds...", flush=True)
            if not await probe_onoff(candidate):
                print(f"New device did not answer an On/Off read; ignoring {candidate.ieee}")
                continue
            print(f"Paired: {describe_device(candidate)}")
            return candidate
    finally:
        print("Closing permit-join and saving the database...", flush=True)
        app.remove_listener(listener)
        try:
            await app.permit(0)
        except Exception:
            pass
        await asyncio.sleep(1.5)
        await shutdown_zigpy_application(app)


def requested_label(argument: str | None) -> str:
    label = argument.strip() if argument else input("New bulb label (for example BULB-L20): ").strip()
    if not label:
        raise RuntimeError("A bulb label is required")
    if any(character in label for character in (";", "\n", "\r")):
        raise RuntimeError("The label cannot contain semicolons or line breaks")
    return label


def database_nwk(ieee: str) -> int | None:
    wanted = normalize_ieee(ieee)
    uri = f"file:{DATABASE.resolve()}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        for stored_ieee, nwk in connection.execute("SELECT ieee, nwk FROM devices_v13"):
            if normalize_ieee(stored_ieee) == wanted:
                return int(nwk)
    return None


async def coordinator_link_key(serial_port: str, ieee: str) -> dict[str, Any] | None:
    znp = ZNP(ControllerApplication.SCHEMA({"device": {"path": serial_port}}))
    await znp.connect()
    try:
        await znp.load_network_info(load_devices=False)
        seed_hex = znp.network_info.stack_specific.get("zstack", {}).get("tclk_seed")
        if not seed_hex:
            return None
        seed = znp_t.KeyData(bytes.fromhex(seed_hex))
        async for key in znp_security.read_hashed_link_keys(znp, seed):
            if normalize_ieee(key.partner_ieee) == normalize_ieee(ieee):
                return {
                    "key": key.key.serialize().hex(),
                    "tx_counter": int(key.tx_counter),
                    "rx_counter": int(key.rx_counter),
                }
        return None
    finally:
        await znp.disconnect()


def add_backup_device(
    backup: dict[str, Any], ieee: str, nwk: int, link_key: dict[str, Any]
) -> dict[str, Any]:
    devices = list(backup["devices"])
    devices.append(
        {
            "ieee_address": normalize_ieee(ieee),
            "nwk_address": f"{nwk:04x}",
            "is_child": True,
            "link_key": link_key,
        }
    )
    devices.sort(key=lambda device: str(device["ieee_address"]))
    return {**backup, "devices": devices}


def refresh_after_pairing(
    serial_port: str, run_dir: Path, ieee: str, nwk: int
) -> Path:
    refreshed_path = run_dir / "coordinator_after_pairing.json"
    print("\nRefreshing the coordinator backup used by the flasher...")
    download_coordinator_backup(
        serial_port,
        refreshed_path,
        run_dir / "coordinator_after_pairing_command.json",
    )
    refreshed = validate_coordinator_backup(refreshed_path)
    if normalize_ieee(ieee) in backup_device_ids(refreshed):
        print("Coordinator backup contains the new bulb.", flush=True)
        return refreshed_path

    print("Recovering the bulb's trust-center key...", flush=True)
    link_key = asyncio.run(coordinator_link_key(serial_port, ieee))
    if link_key is None:
        raise RuntimeError(
            "The paired bulb is missing from the coordinator backup and its "
            "trust-center key could not be recovered. Do not flash dongles."
        )

    repaired_path = run_dir / "coordinator_with_paired_bulb.json"
    write_json(repaired_path, add_backup_device(refreshed, ieee, nwk, link_key))
    print("The coordinator omitted the bulb's address record; repairing it now...", flush=True)
    restore_coordinator_backup(
        serial_port,
        repaired_path,
        run_dir / "repair_coordinator_command.json",
    )
    print("Coordinator restarted; waiting 3 seconds before verification...", flush=True)
    time.sleep(3)

    verified_path = run_dir / "coordinator_after_repair.json"
    print("Verifying the repaired coordinator state...", flush=True)
    download_coordinator_backup(
        serial_port,
        verified_path,
        run_dir / "coordinator_after_repair_command.json",
    )
    verified = validate_coordinator_backup(verified_path)
    if normalize_ieee(ieee) not in backup_device_ids(verified):
        raise RuntimeError(
            "The coordinator repair did not persist the paired bulb. "
            "Do not flash dongles; keep the run directory for recovery."
        )
    return verified_path


def main() -> int:
    args = parse_args()
    assert_runtime_versions()
    if args.permit_seconds < 10:
        raise RuntimeError("--permit-seconds must be at least 10")
    inventory = load_inventory()
    label = requested_label(args.label)
    existing = next(
        (
            item
            for item in inventory["bulbs"]
            if str(item.get("label", "")).casefold() == label.casefold()
        ),
        None,
    )

    if existing is not None:
        ieee = str(existing["ieee"])
        assert_master_data(allow_missing_ieee=ieee)
        master = validate_coordinator_backup(COORDINATOR_BACKUP)
        if normalize_ieee(ieee) in backup_device_ids(master):
            raise RuntimeError(f"Label {label!r} already exists in bulbs.json")
        nwk = database_nwk(ieee)
        if nwk is None:
            raise RuntimeError(
                f"Label {label!r} exists in bulbs.json but is absent from zigpy.db"
            )

        serial_port = select_serial_port(args.serial_port)
        run_dir = new_run_dir("pair-recovery")
        print(f"Resuming coordinator refresh for {label} -> {ieee}")
        refreshed_path = refresh_after_pairing(serial_port, run_dir, ieee, nwk)
        atomic_copy(refreshed_path, COORDINATOR_BACKUP)
        update_overview("paired_new_bulb", data_changed=True)
        print(f"\nRecovered {label} -> {ieee}")
        print(f"Updated flasher data: {COORDINATOR_BACKUP}")
        print(f"Command logs:         {run_dir}")
        return 0

    assert_master_data()

    serial_port = select_serial_port(args.serial_port)
    run_dir = new_run_dir("pair")
    input("Press Enter to pair now: ")
    print("Starting pairing...", flush=True)

    device = asyncio.run(pair_one(serial_port, args.permit_seconds))
    ieee = str(device.ieee).lower()
    if any(normalize_ieee(item.get("ieee")) == normalize_ieee(ieee) for item in inventory["bulbs"]):
        raise RuntimeError(f"The joined bulb {ieee} is already present in bulbs.json")
    inventory["bulbs"].append({"label": label, "ieee": ieee})
    write_inventory(inventory)
    update_overview("pair_pending_coordinator_refresh", data_changed=True)

    nwk = database_nwk(ieee)
    if nwk is None:
        raise RuntimeError(f"The paired bulb {ieee} was not saved to zigpy.db")
    refreshed_path = refresh_after_pairing(serial_port, run_dir, ieee, nwk)
    print("Installing the verified inventory and flasher data...", flush=True)
    atomic_copy(refreshed_path, COORDINATOR_BACKUP)
    update_overview("paired_new_bulb", data_changed=True)

    print(f"\nAdded {label} -> {ieee}")
    print(f"Updated database:           {DATABASE}")
    print(f"Updated inventory:          {INVENTORY}")
    print(f"Updated flasher data:       {COORDINATOR_BACKUP}")
    print(f"Command logs:               {run_dir}")
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
