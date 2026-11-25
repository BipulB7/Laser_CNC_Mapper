# Bow Mapper

Controls the LUNYEE 4040 Nova (Grbl_ESP32 1.3a) with the Micro-Epsilon ILD1320-25 laser on the spindle mount. It scans de-lidded MI300X / MI350 packages point by point and reports the die bow, warpage and tilt.

It runs as a local web app: a Python server on this PC, used from the browser at http://localhost:8765.

## Start

| File | What it does |
|---|---|
| `START_BowMapper.bat` | Normal start. The first run builds `.venv` and installs the packages (needs internet once). |
| `START_Simulator.bat` | Same app with a simulated CNC and laser. No hardware needed. |

You need Windows with **Python 3.12 64-bit**. The 64-bit MEDAQLib DLL is already in `drivers\MEDAQLib.dll`.

To move the app to the CNC PC, copy this whole folder, delete `.venv`, and run `START_BowMapper.bat` there.

## First test on the real machine (no laser needed)

1. Close UGS, LaserGRBL and anything else that might hold the USB port.
2. Start the app. Top bar: **USB serial**, pick the port marked “(CNC?)”, then **Connect**.
   The app holds DTR/RTS low, so the ESP32 does **not** reboot on connect.
3. The banner shows *LOCKED* (homing is on, `$22=1`). Press **⌂ Home**. Z goes up first, then X and Y.
   After homing, the travel limits are worked out from the homed position (`$23`, `$27`, `$130`–`$132`) and shown under *Setup & protection*.
4. Jog:
   - Click the pad for one step.
   - Hold a pad button (steps up to 1 mm) to keep moving; release to stop.
   - Keys: arrows for X/Y, PgUp/PgDn for Z. Hold Shift to move while held.
   - Keys 1–5 pick the step size, 0 is hold mode, Esc stops.
5. Check the directions. If “right” on screen moves the gantry left, tick **Mirror X** (Machine → Display). Do the same for Y.
6. Click the bed map to set a target, then **Go here**. Try **Esc** during a long move: the machine stops and stays homed.

`$120`–`$121` = 50 mm/s² (low acceleration), so a held jog **coasts** after you release it. That is about 2.8 mm at 1000 mm/min and about 6 mm at 1500. The jog panel shows this. Use **Fine** near the part. Steps always stop exactly.

## Laser

**First connection:** close sensorTOOL, then run `TEST_Laser.bat` (no CNC needed). It finds the IF2001/USB port, tries the ILD1320 baud rates, and prints the readings, noise and scale. Compare the mean with sensorTOOL for the same target.

Then, in the app: Laser tab → **ILD1320**, port `auto` (or e.g. `COM5`) → **Connect**. The app remembers the combination that worked.

If it will not open: close sensorTOOL. If it still fails, open sensorTOOL, connect, disconnect and close it, then retry. Let the sensor warm up for 20 minutes before measuring.

- **Reading scale.** MEDAQLib may report 0–25 mm (from the start of the range) or 25–50 mm (from the sensor face). *Auto* decides from the first 200 readings. Distances on screen are always from the sensor face.
- **Auto standoff.** With a valid reading, this moves Z until the laser reads mid-range (37.5 mm).
- **Laser guard.** A downward jog or move is stopped when the surface gets within `guard_mm` (default 1 mm) of the near end of the range.
- **Z floor = here.** Nothing will move the laser below that machine Z.

## Finding the silicon (MI350 OAM, package side up, lid off)

From the OAM CAD (`0-G362_OAM-05282025_ASM`):

| Feature | Size / position | Height step |
|---|---|---|
| Silicon | 49.93 × 59.31 mm, centred on the board | 0.89 mm above the substrate |
| Substrate | 72 × 76.5 mm | — |
| Frame wall | 10–11 mm from each silicon edge | 5.24 mm above the silicon |

With the standoff set on the silicon, every one of these surfaces is within the laser's range.

**The real part, as mounted:**
- The board is turned 90° from the CAD: **59.31 mm along X, 49.93 mm along Y**.
- The die top is flat to about 0.1 mm (a slight dome: 37.50 mm at the centre, about 37.6 mm near the edges).
- Its edge **rolls off over about 0.6 mm**; it isn't a sharp step.
- Past the edge the surface is uneven (38.2–38.6 mm). About 5 mm further out there is a feature 2.2 mm higher.

Scan tab → **Die outline**. Start by zeroing XY roughly at the die centre and setting the standoff on the die. Then use one of these:

1. **Corners by eye** (fast, about ±0.1–0.2 mm)
   1. Jog the red spot onto each corner (0.1 / 0.01 steps near it) and press **= here**, or type work X/Y. Order doesn't matter.
   2. **Use these 4 corners** fits the rectangle.
   3. It reports how far the worst corner is from a true rectangle and how much the diagonals differ, so a mis-placed corner shows up.
2. **Refine with laser** (precise, about ±0.02–0.05 mm). Starting from the current outline, it measures each edge at two points:
   1. 0.25 mm steps from 3 mm inside the edge;
   2. then 0.05 mm steps across the roll-off, compared against the flat die level 1 mm further in, so it can't creep down the slope;
   3. the edge is where a line through the roll-off readings meets the flat die level: the top corner of the die.
3. **Auto find.** Walks out from work zero in ±X and ±Y to get a rough outline, then runs the same refine. Use this when no corners have been entered.

In every case, work zero moves to the die centre and the rotation is stored. The outline is drawn on the bed map in green with corners numbered 1–4. The expected size is only a check: you get a warning if it's more than 1 mm off or looks turned 90°.

**Trace outline** drives the laser spot round the corners at the current Z, so you can compare the red spot with the real die:
- **Stop at corners**: waits at each corner until you press Resume. You can jog while it waits.
- **Offset**: + traces outside the edge, − inside.

Every laser run is saved as a *locate* session. In Results, **Show profiles** plots each edge walk.

**Scan region = outline − inset** fills a scan region inside the outline, with its rotation.

### Standard data collection (full die height map)

Scan tab → **Height map of the die** → **★ Standard data collection**. Use it after the outline has been verified with Trace outline. It fills the recipe with:

| Setting | Value | Why |
|---|---|---|
| Grid | 1 mm pitch, 0.5 mm inside the outline, rotated with the die | about 2,950 points on 59.31 × 49.93 |
| Pattern | raster (same direction every row, 0.5 mm approach) | no row-to-row striping |
| Settle | 400 ms after GRBL reports Idle | the gantry stops ringing |
| Samples | 200 per point | noise is 0.21 µm, so the mean is good to about 0.02 µm |
| Feed | 1000 mm/min | 1 mm hops never reach full speed |
| Max σ | 3 µm, 2 retries | a noisy point is re-measured |
| Max invalid | 5 % | the die should read 100 % valid |
| Drift check | every 100 points | measured and subtracted |

That's about 0.7–1 s per point, roughly 50 min per die.

Before filling, it runs pre-flight checks: outline present, homed, `$1=255`, and the laser near mid-range on the die. Then **Preview on map** → **Start scan**.

## Scanning a die

1. Home. Set the standoff over the die. Put the laser at the die centre (or a corner) and press **Zero XY here**.
2. Scan tab:
   1. Add a region.
   2. Jog to one corner and press **A = here**, then to the opposite corner and press **B = here**.
   3. Set the pitch, then **Preview on map**.
3. **▶ Start scan.** At each point the app:
   - moves and waits until GRBL reports the move finished;
   - waits the settle time;
   - averages N new laser samples;
   - re-measures noisy or out-of-range points (up to *Retries*).

   Every *Drift check every* points it goes back to the first point, and the drift is removed in the analysis. Pause, Resume and Stop are in the top bar.
4. Results tab shows bow (signed: **+ = dome**, centre high), warpage (peak-to-valley after removing the best-fit plane), RMS, tilt, curvature radii, twist, and step heights between regions. Use **Show plot** for 2D, 3D and drift views. Use **Compare with…** to get B − A at matching points.

**Machine baseline.** Scan the reference flat with Type = **Reference flat (baseline)** over the bed area you use. Then choose it under *Subtract baseline*. Its map, after removing its own tilt, is subtracted at each point's machine X/Y. You can also re-apply it to an older scan with *Re-analyse*.

Each scan is saved in `scans\<date-time>_<name>\`:

| File | Contents |
|---|---|
| `points.csv` | One row per point, written as it is measured |
| `drift.csv` | The repeated first-point measurements |
| `meta.json` | Machine, laser and GRBL settings at the time, work offset, status |
| `recipe.json` | The scan settings as run |
| `results.json` | The analysis |

Height = machine Z − laser distance, so a larger height means closer to the laser.

## Programs (Programs tab)

Import by drag and drop. Every program is checked before it runs: limits, Z floor, spindle commands, missing feed rates, line length.

- **G-code** (`.nc .gcode .ngc .tap .txt`) is streamed with GRBL character counting. **Dry run** uses GRBL's own check mode (`$C`) to parse every line with no motion.
- **JSON step program.** The format is in the Programs tab. Available steps:
  - `move`, `move_rel`, `measure`, `dwell`;
  - `pause` (shows a message and waits for Resume);
  - `goto_position`, `set_zero`, `gcode`, `scan`, `repeat`, `home`, `note`.
- **CSV point list**: `x,y[,z][,label][,samples]`. Each row becomes a move plus a measure.

There are examples in `programs\` and `recipes\`.

## Safety behaviour

| Situation | What the app does |
|---|---|
| Not homed | Go-to, programs and scans are refused. Continuous jogs stop after 25 mm. *Trust position* overrides this; use it only if the machine is already homed. |
| Any target outside travel (minus 0.5 mm) or below the Z floor | Refused before it is sent. Jogs are clamped to the limit. |
| Browser closed, tab hidden, or focus lost during a held jog | The jog stops within 0.6 s (watchdog). |
| `M3`/`M4` (spindle on) | Blocked. The laser hangs on the spindle. |
| ALARM:1/3/6–9 (position may be lost) | The running job stops and *homed* is cleared. |
| Reset or USB unplugged | Every waiting command fails cleanly and the job stops. |
| **STOP** (Esc) | Feed hold, then reset. Position is kept. |
| **E-STOP** | Immediate reset. Re-home if it was moving. |

## Machine tab

- App settings: speeds, margins, safe Z, laser, scan defaults, screen mirroring.
- GRBL `$$` table with descriptions and the factory value (2026-11-19 capture). Changed values are highlighted.
- **Back up** / **Restore** settings, and a one-click **$1=255**. This keeps the motors energised between points and is recommended for scanning.

## Development

```
.venv\Scripts\python -m pytest -q tests\test_core.py     # 15 tests against the simulated controller
.venv\Scripts\python -m bowmapper --sim                   # app with simulator
```

| Module | Role |
|---|---|
| `bowmapper/grbl.py` | Serial protocol (reader, `?` poller, character counting) |
| `machine.py` | Safety layer, jogging, jobs |
| `laser.py`, `medaq.py` | ILD1320 acquisition, plus a simulated laser |
| `scan.py`, `analysis.py`, `sessions.py` | Scan engine, maths, files |
| `gcode.py`, `programs.py` | Program checking and running |
| `sim_grbl.py` | Simulated Grbl_ESP32 |
| `server.py`, `static/` | Web server and browser UI |
