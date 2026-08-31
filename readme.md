# Zigbee dongle toolkit

This folder holds the working Zigbee network data and the three scripts used to
maintain it:

- `download_from_dongle.py` reads the coordinator state from the known-good
  dongle and installs it as `data/coordinator_backup.json`.
- `pair_new_bulb.py` pairs one bulb, updates `zigpy.db` and `bulbs.json`, then
  downloads a fresh coordinator backup for the flasher.
- `zigbee_dongle_flasher.py` clones the current coordinator data onto one new
  dongle.

The repository currently contains the schema-v13 database and 19 bulbs,
`BULB-L01` through `BULB-L19`.

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
All three scripts accept `--serial-port`; its default is `auto`.

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

Before permit-join opens, the script:

1. snapshots `zigpy.db`, `bulbs.json`, and `coordinator_backup.json`;
2. downloads the dongle's complete current coordinator state;
3. verifies that the dongle belongs to this network;
4. writes a pairing plan and asks for `PAIR`.

After you type `PAIR`, factory-reset the new bulb. When pairing succeeds, the
script adds its IEEE address to `bulbs.json`, closes the Zigbee application, and
downloads a fresh `coordinator_backup.json`. The flasher therefore uses the new
bulb data without a manual copy step.

If the final coordinator download fails, do not flash more dongles. The bulb and
database may already have changed; keep the run directory and rerun
`download_from_dongle.py` with the same known-good dongle.

## Prepare a new dongle

Connect one new dongle and run:

```text
python zigbee_dongle_flasher.py
```

The flasher always downloads the dongle's original coordinator state first. It
saves the complete backup and a redacted comparison under `runs/`, then asks for
`RESTORE`. If the original backup cannot be captured, restoration does not
start. If the dongle already carries this network, the script keeps its newer
counters and exits without writing.

After a successful restore, unplug and reconnect the dongle before using it.

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
