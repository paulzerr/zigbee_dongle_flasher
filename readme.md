# Zigbee dongle toolkit

This folder holds the working Zigbee network data and the scripts used to
maintain it:

- `download_from_dongle.py` reads the coordinator state from the known-good
  dongle and installs it as `data/coordinator_backup.json`.
- `pair_new_bulb.py` pairs one bulb, updates `zigpy.db` and `bulbs.json`, then
  downloads a fresh coordinator backup for the flasher.
- `remove_bulb.py` removes one bulb from the network, database, inventory, and
  coordinator backup.
- `zigbee_dongle_flasher.py` clones the current coordinator data onto one new
  dongle.
- `test_bulb.py` performs a short red, blue, and green smoke test on one bulb.

The repository currently contains the schema-v13 database and 20 bulbs,
`BULB-L01` through `BULB-L20`.

## Important

`data/coordinator_backup.json` contains the Zigbee network key. This is a local,
private repository. Do not publish it or push it to a public Git host.

The flasher restores coordinator network state; it does not install dongle
firmware. The dongles must already have compatible CC2652 coordinator firmware.

Only connect one dongle while provisioning. Do not power two cloned
coordinators near the same bulbs.

## Setup

Use Python 3.10. On Windows:

```text
py -3.10 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

On Linux:

```text
python3.10 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Run scripts from the repository root so the displayed paths are easy to follow.
All scripts that connect to a dongle accept `--serial-port`; its default is
`auto`.
Every script checks the exact pinned Zigbee package versions before touching the
database or dongle and aborts if the active Python environment does not match.

## Refresh from the known-good dongle

Connect only the known-good dongle, then run:

```text
python download_from_dongle.py
```

The script downloads to a timestamped directory under `runs/`, compares the
result with the installed backup, and asks for `SAVE` before replacing the
flasher data. The previous and downloaded files remain in the run directory.

A dongle does not contain `zigpy.db` or `bulbs.json`; those are computer-side
files. This command refreshes coordinator state only.

## Pair a new bulb

Use the known-good dongle. Keep other unpaired Zigbee devices powered off.

```text
python pair_new_bulb.py --label BULB-L20
```

Press Enter when prompted, then factory-reset the new bulb. When pairing succeeds, the
script adds its IEEE address to `bulbs.json`, closes the Zigbee application, and
downloads a fresh `coordinator_backup.json`. The flasher therefore uses the new
bulb data without a manual copy step. If the dongle omits the bulb's address
record from that backup, the script repairs and verifies the coordinator state.
Rerun the same command to resume an interrupted post-pair repair.

If the final coordinator download fails, do not flash more dongles. The bulb and
database may already have changed; keep the run directory and rerun
`download_from_dongle.py` with the same known-good dongle.

## Remove a bulb

Connect only the known-good dongle, power the bulb, and run:

```text
python remove_bulb.py --label BULB-L20
```

Press Enter when prompted. The script asks the bulb to leave, removes it from
`zigpy.db` and `bulbs.json`, and refreshes `coordinator_backup.json` for the
flasher. It explicitly removes the coordinator's security record; if the
firmware still retains device metadata, a verified cleanup fallback removes it.
An interrupted removal can be rerun with the same label. Do not flash dongles
unless the final verification succeeds.

## Prepare a new dongle

Connect one new dongle and run:

```text
python zigbee_dongle_flasher.py
```

The flasher always downloads the dongle's original coordinator state first. It
saves the complete backup and a redacted comparison under `runs/`, then starts
the restore immediately without asking for confirmation. If that backup fails,
the script records that no recovery backup is available, prints a warning, and
continues flashing. If the dongle already carries this network, the script keeps
its newer counters and exits without writing.

After a successful restore, unplug and reconnect the dongle before using it.

## Test a bulb

Connect a flashed dongle and power exactly one paired bulb. Run:

```text
python test_bulb.py
```

Enter its inventory label, for example `BULB-L14`. The script then directly
blinks the bulb once in each color—red, blue, and green—and leaves it off. Each
pulse lasts 0.175 seconds with a 0.1-second pause. It uses the same simple command
order as the original working test and does not create a state snapshot or
backup. A faint trace of the preceding color may appear while the bulb applies
the next hue. Before sending commands, it reports whether the bulb exists in the
local inventory/database and whether the connected dongle carries the toolkit
network. It still tries the bulb and reports routing failures in plain language.

To recover a dongle from a saved pre-restore backup, use the pinned environment:

```text
python -m zigpy_znp.tools.network_restore COM_PORT --input runs/RUN_NAME/before_restore.json
```

Review the path carefully before running a recovery restore.

## Data files

- `data/zigpy.db`: zigpy schema-v13 application database.
- `data/bulbs.json`: labels and stable bulb IEEE addresses.
- `data/coordinator_backup.json`: coordinator identity, keys, counters, and
  device records used by the flasher.
- `overview.csv`: one human-readable row with the last data change, most recent
  completed operation, bulb count, and paired bulb names.

These files are separate because they serve different consumers:

| File | Purpose | Required for |
|---|---|---|
| `zigpy.db` | Detailed computer-side Zigbee application state: devices, endpoints, clusters, and attributes | Running and controlling the Zigbee network through zigpy |
| `bulbs.json` | Toolkit-specific human labels such as `BULB-L20`, mapped to IEEE addresses | Selecting bulbs by a stable, readable name; this file is convenient rather than fundamental to Zigbee |
| `coordinator_backup.json` | Portable coordinator identity, network credentials, counters, and device/link-key records | Cloning the network onto another dongle |

`zigpy.db` cannot replace the coordinator backup because it does not contain a
restorable copy of all dongle state. The coordinator backup cannot replace
`zigpy.db` because it does not contain zigpy's application model. `bulbs.json`
could technically be replaced by another label store, but the toolkit keeps it
as a small, explicit inventory.

The scripts maintain `overview.csv`. Detailed snapshots and command output are
kept under `runs/` and ignored by Git because they can contain sensitive keys.

## Versioning data changes

The repository starts with a short commit history separating setup, coordinator
tools, pairing data, and documentation. After pairing, review the changes before
committing them:

```text
git status
git diff -- overview.csv data/bulbs.json
git add data/zigpy.db data/bulbs.json data/coordinator_backup.json overview.csv
git commit -m "data: add BULB-L20"
```

Do not commit a pairing attempt until the post-pairing coordinator download has
succeeded.
