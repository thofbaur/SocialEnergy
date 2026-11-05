# SocialEnergy

Postprocessing of the contact data of the SocialEnergy beacons (DSA2026
deployment). The beacons record each other over Bluetooth, a Base station
reads them out, and a logger PC writes what it receives into `*.log` files.
The scripts here turn these logs into dated, checked data for the evaluation:
contacts, self-reports, eco sessions, reboots, battery readings and the
coverage of every beacon (recorded, lost and why).

## Contents

| Path | What |
| --- | --- |
| `Postprocessing/` | The pipeline and its checks; usage in `Postprocessing/POSTPROCESSING.md` |
| `Postprocessing/Postprocessing Method.md` | The method in detail, with a dated log of every change and its measured effect |
| `Background Information.md` | The system and the known issues of the logs, without knowledge of the firmware |
| `ToDo.md` | Open and done points of the postprocessing and the evaluation |

The firmware, the logger and their open points (`ToDo.md` there) are in the
separate firmware repository (`BeaconNRF54`).

## Quick start

```sh
cd Postprocessing
pip install -r requirements.txt     # Python >= 3.11
python run_pipeline.py --reports    # original logs -> Output/data/ (about 20 min)
```

The inputs of a capture (original logs, beacon lists, `study.toml`,
`logger.toml`) live in a study folder outside this repository, by default
`../Studies/DSA2026`; another one is given with `--study-dir <folder>`. The
code holds no values of a study.

## License

See `LICENSE`.
