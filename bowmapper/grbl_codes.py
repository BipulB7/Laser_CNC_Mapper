"""Human-readable GRBL error, alarm and setting descriptions (Grbl 1.1 / Grbl_ESP32 1.3)."""

ERRORS = {
    1: "Expected command letter: a G-code word was missing its letter.",
    2: "Bad number format: a value is missing or malformed.",
    3: "'$' command not recognised.",
    4: "Negative value where a positive one is required.",
    5: "Homing is not enabled ($22=0).",
    6: "Step pulse must be at least 3 microseconds.",
    7: "Settings read failed; defaults were restored.",
    8: "Controller must be Idle for this '$' command (it is moving, holding or in alarm).",
    9: "G-code is locked during an alarm or a jog. Unlock ($X) or home ($H) first.",
    10: "Soft limits need homing enabled.",
    11: "Line too long (max 80 characters).",
    12: "Step rate too high for this setting.",
    13: "Safety door is open.",
    14: "Startup/build line too long.",
    15: "Jog target is outside the machine travel.",
    16: "Invalid jog command (only G20/G21/G90/G91/G53 and axis words with F allowed).",
    17: "Laser mode needs PWM output.",
    20: "Unsupported or invalid G-code command.",
    21: "Two commands from the same modal group on one line.",
    22: "Feed rate has not been set (add F...).",
    23: "Command needs an integer value.",
    24: "Two commands on the line both need axis words.",
    25: "A G-code word is repeated on the line.",
    26: "No axis words found for a command that needs them.",
    27: "Line number out of range.",
    28: "A required value word is missing.",
    29: "G59.x work coordinate systems are not supported.",
    30: "G53 only works with G0 and G1.",
    31: "Axis words given on a line that does not use them.",
    32: "Arc (G2/G3) needs at least one in-plane axis word.",
    33: "Motion target is invalid.",
    34: "Arc radius is invalid.",
    35: "Arc (G2/G3) needs at least one in-plane offset word.",
    36: "Unused value words on the line.",
    37: "Tool length offset is not on the tool length axis.",
    38: "Tool number too large.",
}

ALARMS = {
    1: ("Hard limit switch hit. Position is lost: home the machine again.", True),
    2: ("Soft limit: a move would leave the machine travel. Position kept; unlock to continue.", False),
    3: ("Reset while moving. Steps may be lost: home the machine again.", True),
    4: ("Probe was already triggered before probing.", False),
    5: ("Probe did not touch within the travel.", False),
    6: ("Homing failed: reset during the homing cycle.", True),
    7: ("Homing failed: safety door opened during homing.", True),
    8: ("Homing failed: could not clear the limit switch on pull-off. Check $27 and the switch.", True),
    9: ("Homing failed: limit switch not found within travel. Check switches and wiring.", True),
    10: ("Homing failed (controller-specific). Check switches and wiring.", True),
}


def error_text(code) -> str:
    try:
        return ERRORS.get(int(code), f"GRBL error {code}")
    except (TypeError, ValueError):
        return f"GRBL error {code}"


def alarm_text(code) -> str:
    try:
        return ALARMS.get(int(code), (f"Alarm {code}", True))[0]
    except (TypeError, ValueError):
        return f"Alarm {code}"


def alarm_loses_position(code) -> bool:
    try:
        return ALARMS.get(int(code), ("", True))[1]
    except (TypeError, ValueError):
        return True


# $-setting descriptions: (name, unit, note)
SETTINGS = {
    0: ("Step pulse time", "us", ""),
    1: ("Step idle delay", "ms", "255 keeps motors energised (recommended for scanning)"),
    2: ("Step pulse invert", "mask", ""),
    3: ("Step direction invert", "mask", ""),
    4: ("Invert step enable pin", "bool", ""),
    5: ("Invert limit pins", "bool", ""),
    6: ("Invert probe pin", "bool", ""),
    10: ("Status report options", "mask", "1 = report machine position"),
    11: ("Junction deviation", "mm", ""),
    12: ("Arc tolerance", "mm", ""),
    13: ("Report in inches", "bool", "keep 0 (mm)"),
    20: ("Soft limits enable", "bool", "needs homing; this app also enforces limits itself"),
    21: ("Hard limits enable", "bool", ""),
    22: ("Homing cycle enable", "bool", ""),
    23: ("Homing direction invert", "mask", "bit set = axis homes toward negative"),
    24: ("Homing locate feed", "mm/min", ""),
    25: ("Homing search seek", "mm/min", ""),
    26: ("Homing switch debounce", "ms", ""),
    27: ("Homing switch pull-off", "mm", ""),
    30: ("Max spindle speed", "RPM", ""),
    31: ("Min spindle speed", "RPM", ""),
    32: ("Laser mode", "bool", "keep 0"),
    39: ("Enable legacy RT commands", "bool", ""),
    100: ("X steps/mm", "steps/mm", ""),
    101: ("Y steps/mm", "steps/mm", ""),
    102: ("Z steps/mm", "steps/mm", ""),
    103: ("A steps/mm", "steps/mm", ""),
    110: ("X max rate", "mm/min", ""),
    111: ("Y max rate", "mm/min", ""),
    112: ("Z max rate", "mm/min", ""),
    113: ("A max rate", "mm/min", ""),
    120: ("X acceleration", "mm/s2", ""),
    121: ("Y acceleration", "mm/s2", ""),
    122: ("Z acceleration", "mm/s2", ""),
    123: ("A acceleration", "mm/s2", ""),
    130: ("X max travel", "mm", ""),
    131: ("Y max travel", "mm", ""),
    132: ("Z max travel", "mm", ""),
    133: ("A max travel", "mm", ""),
}

# Settings this app suggests for step-and-settle scanning.
RECOMMENDED = {
    1: ("255", "Keep motors energised between points so the head cannot drift or relax."),
}
