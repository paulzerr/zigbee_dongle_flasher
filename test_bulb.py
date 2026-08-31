from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

import zigpy.config as zigpy_conf
from zigpy.device import Device
from zigpy.zcl.clusters.general import LevelControl, OnOff
from zigpy.zcl.clusters.lighting import Color
from zigpy_znp.zigbee.application import ControllerApplication

from toolkit_common import (
    DATABASE,
    INVENTORY,
    assert_master_data,
    load_inventory,
    normalize_ieee,
    select_serial_port,
    update_overview,
)


COLORS = (
    ("red", 0, 254),
    ("blue", 169, 254),
    ("green", 85, 254),
)
BRIGHTNESS = 180
ON_SECONDS = 0.175
OFF_SECONDS = 0.1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Blink one paired bulb red, blue, and green.")
    parser.add_argument("--bulb-id", help="Inventory label. Asked interactively when omitted.")
    parser.add_argument("--serial-port", default="auto", help="Serial port or 'auto'.")
    return parser.parse_args()


def find_input_cluster(device: Device, cluster_id: int) -> Any:
    if hasattr(device, "find_cluster"):
        return device.find_cluster(cluster_id)
    for endpoint in device.endpoints.values():
        cluster = getattr(endpoint, "in_clusters", {}).get(cluster_id)
        if cluster is not None:
            return cluster
    raise RuntimeError(f"Bulb has no input cluster 0x{cluster_id:04x}")


def target_from_inventory(label: str) -> str:
    for bulb in load_inventory(INVENTORY)["bulbs"]:
        if str(bulb.get("label", "")).casefold() == label.casefold():
            return str(bulb["ieee"])
    raise RuntimeError(f"Bulb ID {label!r} is not in data/bulbs.json")


def find_device(app: ControllerApplication, ieee: str) -> Device:
    wanted = normalize_ieee(ieee)
    for device in app.devices.values():
        if normalize_ieee(device.ieee) == wanted:
            return device
    raise RuntimeError(f"Bulb {ieee} is not present in data/zigpy.db")


def build_config(serial_port: str, database: Path) -> dict[str, Any]:
    return {
        zigpy_conf.CONF_DEVICE: {
            zigpy_conf.CONF_DEVICE_PATH: serial_port,
            zigpy_conf.CONF_DEVICE_BAUDRATE: 115200,
            zigpy_conf.CONF_DEVICE_FLOW_CONTROL: None,
        },
        zigpy_conf.CONF_DATABASE: str(database.resolve()),
        zigpy_conf.CONF_NWK_BACKUP_ENABLED: False,
        zigpy_conf.CONF_OTA: {zigpy_conf.CONF_OTA_ENABLED: False},
    }


async def run_test(serial_port: str, label: str, ieee: str) -> None:
    app = await ControllerApplication.new(build_config(serial_port, DATABASE))
    try:
        device = find_device(app, ieee)
        task = device.schedule_initialize()
        if task is not None:
            await asyncio.wait_for(task, timeout=20)

        color = find_input_cluster(device, Color.cluster_id)
        level = find_input_cluster(device, LevelControl.cluster_id)
        onoff = find_input_cluster(device, OnOff.cluster_id)

        print(f"Bulb:  {label} ({ieee})")
        print(f"Dongle: {serial_port}")
        for name, hue, saturation in COLORS:
            print(name)
            await onoff.on(expect_reply=True)
            await color.move_to_hue_and_saturation(
                hue=hue,
                saturation=saturation,
                transition_time=1,
                expect_reply=True,
            )
            await asyncio.sleep(0.15)
            await level.move_to_level_with_on_off(
                level=BRIGHTNESS,
                transition_time=1,
                expect_reply=True,
            )
            await asyncio.sleep(ON_SECONDS)
            await onoff.off(expect_reply=True)
            await asyncio.sleep(OFF_SECONDS)
    finally:
        await app.shutdown()


def main() -> int:
    args = parse_args()
    assert_master_data()
    label = (args.bulb_id or input("Bulb ID (for example BULB-L14): ")).strip()
    if not label:
        raise RuntimeError("A bulb ID is required")
    ieee = target_from_inventory(label)
    serial_port = select_serial_port(args.serial_port)
    asyncio.run(run_test(serial_port, label, ieee))
    update_overview(f"tested_{label}", data_changed=False)
    print("RGB bulb test finished.")
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
