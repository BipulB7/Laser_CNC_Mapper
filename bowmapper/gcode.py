"""G-code cleaning, interpretation and validation (for preview and safety checks).

The interpreter tracks what GRBL will do with a program: modal state
(G90/G91, G20/G21, motion mode, feed), work offsets (G10 L2/L20, G92), G53
machine moves and arcs. It produces line segments in MACHINE coordinates so
the program can be drawn on the bed map and every point checked against the
travel limits and Z floor before anything is sent.
"""

from __future__ import annotations

import math
import re

WORD_RE = re.compile(r"([A-Z])([-+]?(?:\d+\.?\d*|\.\d+))")
MAX_LINE = 80
MAX_PREVIEW_SEGMENTS = 60000


def clean_line(raw: str) -> str:
    """Remove comments/whitespace, upper-case. '' for blank or '%' lines."""
    s = re.sub(r"\(.*?\)", "", raw.split(";")[0])
    s = s.replace(" ", "").replace("\t", "").strip().upper()
    if s in ("%",):
        return ""
    return s


class GcodeInterpreter:
    def __init__(self, wco=None, start=None, rapid_rate: float = 4000.0, block_spindle: bool = True):
        self.pos = list(start or [0.0, 0.0, 0.0])          # machine coords
        self.wco = list(wco or [0.0, 0.0, 0.0])            # current G54 + G92 total
        self.g92 = [0.0, 0.0, 0.0]
        self.units = 21
        self.dist = 90
        self.motion = 0
        self.feed = 0.0
        self.plane = 17
        self.rapid_rate = rapid_rate
        self.block_spindle = block_spindle
        self.segments: list[tuple] = []                     # (x0,y0,z0,x1,y1,z1,kind,line_no)
        self.errors: list[dict] = []
        self.warnings: list[dict] = []
        self.time_s = 0.0
        self.length = {"feed": 0.0, "rapid": 0.0}
        self.flags: set[str] = set()

    # ------------------------------------------------------------------
    def _err(self, n, text):
        self.errors.append({"line": n, "text": text})

    def _warn(self, n, text):
        if len(self.warnings) < 200:
            self.warnings.append({"line": n, "text": text})

    def feed_line(self, s: str, n: int) -> None:
        if not s:
            return
        if s.startswith("$"):
            self._err(n, "System ($) commands are not allowed inside a program. Use a JSON program step instead.")
            return
        if len(s) > MAX_LINE:
            self._err(n, f"Line is {len(s)} characters; GRBL accepts at most {MAX_LINE}.")
            return
        words = []
        pos = 0
        for m in WORD_RE.finditer(s):
            if m.start() != pos:
                self._err(n, f"Cannot read '{s[pos:m.start() + 1]}'")
                return
            words.append((m.group(1), float(m.group(2))))
            pos = m.end()
        if pos != len(s):
            self._err(n, f"Cannot read '{s[pos:]}'")
            return

        g53 = False
        motion = None
        nonmodal = None
        vals: dict[str, float] = {}
        for letter, v in words:
            if letter == "G":
                code = round(v, 1)
                if code in (0, 1, 2, 3):
                    motion = int(code)
                elif code in (38.2, 38.3, 38.4, 38.5):
                    self._err(n, "Probing (G38.x) is not supported here.")
                elif code == 4:
                    nonmodal = "dwell"
                elif code == 10:
                    nonmodal = "g10"
                elif code in (17, 18, 19):
                    self.plane = int(code)
                    if code != 17:
                        self._warn(n, f"G{int(code)} plane: arcs are previewed approximately.")
                elif code in (20, 21):
                    self.units = int(code)
                    if code == 20:
                        self._warn(n, "Program uses inches (G20).")
                elif code in (28, 30):
                    nonmodal = "home_pos"
                elif code in (28.1, 30.1):
                    pass
                elif code == 53:
                    g53 = True
                elif code == 54:
                    pass
                elif code in (55, 56, 57, 58, 59):
                    self._warn(n, f"G{int(code)} selects another work system; preview assumes G54.")
                elif code in (90, 91):
                    self.dist = int(code)
                elif code in (90.1, 91.1, 40, 49, 43.1, 80, 94, 61, 64):
                    pass
                elif code == 93:
                    self._warn(n, "Inverse-time feed (G93): time estimate is approximate.")
                elif code == 92:
                    nonmodal = "g92"
                elif code == 92.1:
                    self.g92 = [0.0, 0.0, 0.0]
                else:
                    self._err(n, f"G{v:g} is not supported by GRBL.")
            elif letter == "M":
                code = int(v)
                if code in (3, 4):
                    self.flags.add("spindle")
                    if self.block_spindle:
                        self._err(n, f"M{code} (spindle on) is blocked: the laser is mounted on the spindle.")
                elif code == 6:
                    self._err(n, "M6 tool change: this machine has no tool changer.")
                elif code in (0, 1):
                    self.flags.add("pause")
                    self._warn(n, f"M{code}: the program pauses here until you press Resume.")
                elif code in (2, 30):
                    self.flags.add("end")
                elif code in (5, 7, 8, 9):
                    pass
                else:
                    self._warn(n, f"M{code} is not a GRBL command; GRBL may reject it.")
            elif letter == "N":
                pass
            else:
                if letter in vals:
                    self._err(n, f"Word {letter} appears twice.")
                vals[letter] = v

        k = 25.4 if self.units == 20 else 1.0
        if "F" in vals:
            self.feed = vals["F"] * k
        if motion is not None:
            self.motion = motion
        axes = {a: vals[a] * k for a in "XYZ" if a in vals}

        if nonmodal == "dwell":
            self.time_s += vals.get("P", 0.0)
            return
        if nonmodal == "g10":
            l = vals.get("L")
            if l not in (2, 20):
                self._err(n, "G10 needs L2 or L20.")
                return
            if vals.get("P", 1) not in (0, 1):
                self._warn(n, "G10 for a work system other than G54; preview assumes G54.")
            for i, a in enumerate("XYZ"):
                if a in axes:
                    if l == 2:
                        self.wco[i] = axes[a] + self.g92[i]
                    else:
                        self.wco[i] = self.pos[i] - axes[a]
            self._warn(n, "Program changes the work zero (G10).")
            return
        if nonmodal == "g92":
            for i, a in enumerate("XYZ"):
                if a in axes:
                    old = self.wco[i] - self.g92[i]
                    self.g92[i] = self.pos[i] - old - axes[a]
                    self.wco[i] = old + self.g92[i]
            self._warn(n, "Program sets a G92 offset.")
            return
        if nonmodal == "home_pos":
            self._warn(n, "G28/G30 moves to a stored position the preview cannot see.")
            return
        if not axes:
            return
        if g53 and self.motion not in (0, 1):
            self._err(n, "G53 only works with G0/G1.")
            return

        tgt = list(self.pos)
        for i, a in enumerate("XYZ"):
            if a in axes:
                if g53:
                    tgt[i] = axes[a]
                elif self.dist == 91:
                    tgt[i] = self.pos[i] + axes[a]
                else:
                    tgt[i] = axes[a] + self.wco[i]
        if self.motion in (1, 2, 3) and self.feed <= 0:
            self._err(n, "Feed rate not set (add F...).")
            return
        if self.motion in (2, 3):
            self._arc(n, tgt, vals, k)
        else:
            self._seg(n, self.pos, tgt, 0 if self.motion == 0 else 1)
        self.pos = tgt

    def _seg(self, n, a, b, kind):
        d = math.dist(a, b)
        if d < 1e-9:
            return
        if kind == 0:
            self.length["rapid"] += d
            self.time_s += d / self.rapid_rate * 60
        else:
            self.length["feed"] += d
            self.time_s += d / max(self.feed, 1e-6) * 60
        self.segments.append((a[0], a[1], a[2], b[0], b[1], b[2], kind, n))

    def _arc(self, n, tgt, vals, k):
        if self.plane != 17:
            self._seg(n, self.pos, tgt, 1)
            return
        x0, y0 = self.pos[0], self.pos[1]
        x1, y1 = tgt[0], tgt[1]
        cw = self.motion == 2
        if "R" in vals:
            r = vals["R"] * k
            dx, dy = x1 - x0, y1 - y0
            h2 = r * r - (dx * dx + dy * dy) / 4
            if h2 < -1e-6:
                self._err(n, "Arc radius is too small for the end point.")
                return
            h = math.sqrt(max(h2, 0))
            d = math.hypot(dx, dy) or 1e-9
            sgn = -1 if cw else 1
            if r < 0:
                sgn = -sgn
            cx = (x0 + x1) / 2 - sgn * h * dy / d
            cy = (y0 + y1) / 2 + sgn * h * dx / d
        else:
            if "I" not in vals and "J" not in vals:
                self._err(n, "Arc needs I/J or R.")
                return
            cx = x0 + vals.get("I", 0.0) * k
            cy = y0 + vals.get("J", 0.0) * k
        r0 = math.hypot(x0 - cx, y0 - cy)
        a0 = math.atan2(y0 - cy, x0 - cx)
        a1 = math.atan2(y1 - cy, x1 - cx)
        if cw:
            sweep = a0 - a1
            if sweep <= 1e-9:
                sweep += 2 * math.pi
            sweep = -sweep
        else:
            sweep = a1 - a0
            if sweep <= 1e-9:
                sweep += 2 * math.pi
        steps = max(8, int(abs(sweep) / math.radians(3)))
        prev = list(self.pos)
        for s in range(1, steps + 1):
            t = s / steps
            ang = a0 + sweep * t
            p = [cx + r0 * math.cos(ang), cy + r0 * math.sin(ang), self.pos[2] + (tgt[2] - self.pos[2]) * t]
            if s == steps:
                p = list(tgt)
            self._seg(n, prev, p, 1)
            prev = p


def analyze_gcode(text: str, wco, start, allowed: dict | None, homed: bool,
                  rapid_rate: float = 4000.0, block_spindle: bool = True) -> dict:
    """Clean + interpret + check limits. Returns everything the UI needs."""
    it = GcodeInterpreter(wco=wco, start=start, rapid_rate=rapid_rate, block_spindle=block_spindle)
    lines: list[str] = []
    src: list[int] = []
    for i, raw in enumerate(text.splitlines(), start=1):
        s = clean_line(raw)
        if not s:
            continue
        it.feed_line(s, i)
        lines.append(s)
        src.append(i)
    segs = it.segments
    out_of_bounds = []
    bounds = None
    if segs:
        xs = [v for s in segs for v in (s[0], s[3])]
        ys = [v for s in segs for v in (s[1], s[4])]
        zs = [v for s in segs for v in (s[2], s[5])]
        bounds = {"min": [min(xs), min(ys), min(zs)], "max": [max(xs), max(ys), max(zs)]}
        if allowed and homed:
            for s in segs:
                for p in ((s[3], s[4], s[5]),):
                    for i, a in enumerate("xyz"):
                        lo, hi = allowed[a]
                        if p[i] < lo - 1e-6 or p[i] > hi + 1e-6:
                            out_of_bounds.append({"line": s[7], "text": f"{a.upper()} {p[i]:.3f} is outside the allowed range {lo:.2f}..{hi:.2f} (machine)."})
                            break
                if len(out_of_bounds) >= 20:
                    break
    errors = it.errors + out_of_bounds
    if not homed:
        it.warnings.insert(0, {"line": 0, "text": "Machine not homed: limits could not be checked."})
    preview = segs
    if len(preview) > MAX_PREVIEW_SEGMENTS:
        step = len(preview) / MAX_PREVIEW_SEGMENTS
        preview = [preview[int(i * step)] for i in range(MAX_PREVIEW_SEGMENTS)]
    return {
        "kind": "gcode",
        "ok": not errors,
        "errors": errors[:200],
        "warnings": it.warnings,
        "lines": lines,
        "source_lines": src,
        "n_lines": len(lines),
        "bounds": bounds,
        "est_s": round(it.time_s, 1),
        "length_mm": {k: round(v, 1) for k, v in it.length.items()},
        "flags": sorted(it.flags),
        "preview": [[round(s[0], 3), round(s[1], 3), round(s[3], 3), round(s[4], 3), s[6]] for s in preview],
    }
