from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

import zigpy.config as zigpy_conf
import zigpy.types as zigpy_types
import zigpy.zdo
import zigpy.zdo.types as zdo_types
from zigpy.device import Device
from zigpy.zcl.clusters.general import LevelControl, OnOff
from zigpy.zcl.clusters.lighting import Color
from zigpy_znp.zigbee.application import ControllerApplication

from toolkit_common import (
    DATABASE,
    INVENTORY,
    assert_master_data,
    load_inventory,
    new_run_dir,
    normalize_ieee,
    require_confirmation,
    select_serial_port,
    snapshot_master_data,
    update_overview,
    write_json,
)


COLORS = (
    ("red", 0, 254),
    ("blue", 169, 254),
    ("green", 85, 254),
)
TEST_BRIGHTNESS = 180
PREP_BRIGHTNESS = 1
ON_SECONDS = 0.175
OFF_SECONDS = 0.1
COLOR_SETTLE_SECONDS = 0.05
COLOR_ATTEMPTS = 3
COLOR_TRANSITION_TENTHS = 1


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


async def read_state(device: Device) -> dict[str, int | bool]:
    requests = (
        (find_input_cluster(device, OnOff.cluster_id), ("on_off",)),
        (find_input_cluster(device, LevelControl.cluster_id), ("current_level",)),
        (
            find_input_cluster(device, Color.cluster_id),
            ("current_hue", "current_saturation"),
        ),
    )
    state: dict[str, int | bool] = {}
    for cluster, attributes in requests:
        success, _ = await asyncio.wait_for(
            cluster.read_attributes(list(attributes), allow_cache=False),
            timeout=10,
        )
        for attribute in attributes:
            if attribute not in success:
                raise RuntimeError(f"Could not capture bulb attribute {attribute}")
            value = success[attribute]
            state[attribute] = bool(value) if attribute == "on_off" else int(value)
    return state


async def refresh_address(app: ControllerApplication, ieee: str) -> None:
    await zigpy.zdo.broadcast(
        app=app,
        command=zdo_types.ZDOCmd.NWK_addr_req,
        grpid=None,
        radius=0,
        IEEEAddrOfInterest=zigpy_types.EUI64.convert(ieee),
        RequestType=zdo_types.AddrRequestType.Single,
        StartIndex=0,
    )
    await asyncio.sleep(1)


async def capture_state(
    app: ControllerApplication,
    device: Device,
    ieee: str,
) -> dict[str, int | bool]:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            return await read_state(device)
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                print(f"No response yet; refreshing the bulb route ({attempt}/2)...")
                await refresh_address(app, ieee)
    raise RuntimeError("The selected bulb did not answer the state read") from last_error


async def restore_state(device: Device, state: dict[str, int | bool]) -> None:
    color = find_input_cluster(device, Color.cluster_id)
    level = find_input_cluster(device, LevelControl.cluster_id)
    onoff = find_input_cluster(device, OnOff.cluster_id)
    await onoff.on(expect_reply=True)
    await color.move_to_hue_and_saturation(
        hue=state["current_hue"],
        saturation=state["current_saturation"],
        transition_time=1,
        expect_reply=True,
    )
    await level.move_to_level_with_on_off(
        level=state["current_level"],
        transition_time=1,
        expect_reply=True,
    )
    if not state["on_off"]:
        await onoff.off(expect_reply=True)


async def apply_and_verify_color(
    color: Any,
    name: str,
    hue: int,
    saturation: int,
) -> dict[str, int | str]:
    observed_hue = -1
    observed_saturation = -1
    for attempt in range(1, COLOR_ATTEMPTS + 1):
        await color.move_to_hue_and_saturation(
            hue=hue,
            saturation=saturation,
            transition_time=COLOR_TRANSITION_TENTHS,
            expect_reply=True,
        )
        await asyncio.sleep(COLOR_SETTLE_SECONDS)
        success, _ = await color.read_attributes(
            ["current_hue", "current_saturation"],
            allow_cache=False,
        )
        observed_hue = int(success["current_hue"])
        observed_saturation = int(success["current_saturation"])
        hue_error = min(abs(observed_hue - hue), 255 - abs(observed_hue - hue))
        if hue_error <= 3 and abs(observed_saturation - saturation) <= 3:
            return {
                "name": name,
                "hue": observed_hue,
                "saturation": observed_saturation,
                "attempts": attempt,
            }
    raise RuntimeError(
        f"{name} was not applied after {COLOR_ATTEMPTS} attempts: "
        f"hue={observed_hue}, saturation={observed_saturation}"
    )


async def run_test(
    serial_port: str,
    database: Path,
    label: str,
    ieee: str,
    run_dir: Path,
) -> None:
    app = await ControllerApplication.new(build_config(serial_port, database))
    mutation_started = False
    try:
        device = find_device(app, ieee)
        task = device.schedule_initialize()
        if task is not None:
            await asyncio.wait_for(task, timeout=20)

        before = await capture_state(app, device, ieee)
        write_json(run_dir / "before_state.json", {"bulb_id": label, "ieee": ieee, **before})
        write_json(
            run_dir / "blink_plan.json",
            {
                "bulb_id": label,
                "ieee": ieee,
                "sequence": [name for name, _, _ in COLORS],
                "brightness": TEST_BRIGHTNESS,
                "on_seconds": ON_SECONDS,
                "off_seconds": OFF_SECONDS,
                "color_preparation_brightness": PREP_BRIGHTNESS,
                "color_transition_seconds": COLOR_TRANSITION_TENTHS / 10,
                "restore_state": before,
            },
        )

        print(f"\nCaptured state: {before}")
        print("Plan: red blink, blue blink, green blink, then restore the captured state.")
        print(f"Saved state and plan: {run_dir}")
        require_confirmation("BLINK", "Type BLINK to run the test: ")

        color = find_input_cluster(device, Color.cluster_id)
        level = find_input_cluster(device, LevelControl.cluster_id)
        onoff = find_input_cluster(device, OnOff.cluster_id)
        observed_colors: list[dict[str, int | str]] = []
        sequence_error: Exception | None = None
        after: dict[str, int | bool] | None = None
        mutation_started = True
        try:
            await level.move_to_level_with_on_off(
                level=PREP_BRIGHTNESS,
                transition_time=0,
                expect_reply=True,
            )
            await onoff.off(expect_reply=True)
            await asyncio.sleep(OFF_SECONDS)
            for name, hue, saturation in COLORS:
                print(name)
                await level.move_to_level_with_on_off(
                    level=PREP_BRIGHTNESS,
                    transition_time=0,
                    expect_reply=True,
                )
                observed_colors.append(
                    await apply_and_verify_color(color, name, hue, saturation)
                )
                write_json(run_dir / "observed_colors.json", observed_colors)
                await level.move_to_level_with_on_off(
                    level=TEST_BRIGHTNESS,
                    transition_time=0,
                    expect_reply=True,
                )
                await asyncio.sleep(ON_SECONDS)
                await level.move_to_level_with_on_off(
                    level=PREP_BRIGHTNESS,
                    transition_time=0,
                    expect_reply=True,
                )
                await onoff.off(expect_reply=True)
                await asyncio.sleep(OFF_SECONDS)
        except Exception as exc:
            sequence_error = exc
        finally:
            if mutation_started:
                await restore_state(device, before)
                after = await read_state(device)
                write_json(
                    run_dir / "after_state.json",
                    {"bulb_id": label, "ieee": ieee, **after},
                )

        write_json(run_dir / "observed_colors.json", observed_colors)
        if after != before:
            raise RuntimeError(f"Test completed, but state restoration differs: {after}")
        print(f"Restored and verified: {after}")
        if sequence_error is not None:
            raise sequence_error
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
    run_dir = new_run_dir("test-bulb")
    snapshot_master_data(run_dir / "before")
    runtime_database = run_dir / "before" / DATABASE.name

    print(f"Bulb:  {label} ({ieee})")
    print(f"Dongle: {serial_port}")
    asyncio.run(run_test(serial_port, runtime_database, label, ieee, run_dir))
    update_overview(f"tested_{label}", data_changed=False)
    print("RGB bulb test passed.")
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
