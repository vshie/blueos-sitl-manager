"""A virtual seafloor, and the Ping sonar that sounds it as the simulated vehicle moves.

A Ping reaches everything that uses it through mavlink2rest: BlueOS's Ping service posts each
reading there as a DISTANCE_SENSOR from component 194, Simple Ping Survey reads it back from
that component, and the autopilot, its first rangefinder set to MAVLink, takes it as its own and
reports it on to Cockpit. This sends the same message from the same component, so none of them
can tell the simulated sonar from a real one.

What it sounds is a depth grid the browser built from a Simple Ping Survey log: a survey that
was really run, played back wherever the simulated vehicle drives across it. The grid is depth
below a transducer, which is what the log recorded, so it is sent as the range without anything
added for the hull.
"""

import array
import asyncio
import base64
import binascii
import json
import math
import random
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from sitl_manager import autopilot, custom_presets, mavlink
from sitl_manager.custom_presets import (
    PresetConflictError,
    PresetStore,
    UnknownPresetError,
)
from sitl_manager.models import (
    Seafloor,
    SeafloorUpload,
    SonarNoise,
    SonarReading,
    SonarSettings,
    SonarState,
)
from sitl_manager.settings import (
    DEFAULT_SYSTEM_ID,
    SEAFLOOR_DIR,
    SONAR_FILE,
    VEHICLE_READY_TIMEOUT,
)

# The flat projection the grid is laid out on, shared with the browser that builds it: metres
# per degree of latitude on the WGS84 equator, and of longitude that times cos(latitude). Over
# the few kilometres a survey covers, its error is far below a cell.
METERS_PER_DEGREE = 111_319.49

# A cell the survey never reached, which the beam finds no bottom in.
NO_DATA = 0xFFFF

# The browser coarsens its cells to stay under this, which keeps a grid to 2 MB on disk and in
# memory, inside the container's budget with room to spare.
MAX_CELLS = 1_000_000

GRID_SUFFIX = ".u16"

STORE: PresetStore[Seafloor] = PresetStore(SEAFLOOR_DIR, Seafloor)

# The component BlueOS's Ping service speaks as (MAV_COMP_ID_ONBOARD_COMPUTER4), which is where
# Simple Ping Survey looks for a Ping first.
SONAR_COMPONENT_ID = 194

# The limits and beam BlueOS's Ping service reports for a Ping. The beam is a 30 degree cone.
MIN_DISTANCE_CM = 20
MAX_DISTANCE_CM = 12000
BEAM_WIDTH = 0.52
BEAM_HALF_ANGLE = BEAM_WIDTH / 2

# What the sonar sends where it finds no bottom: off the grid, or over a hole in it. A Ping that
# loses the bottom still answers, at a confidence that says not to believe it, and so does this.
# Falling silent instead would leave the autopilot with no data, and it refuses to arm on that;
# the full range reads as out of range, which it does not mind.
NO_BOTTOM_CM = MAX_DISTANCE_CM
NO_BOTTOM_CONFIDENCE = 1

# A wide beam over a fine grid would take thousands of cells a tick, so a wide one is sampled.
MAX_BEAM_CELLS = 400

# Spread of the range noise, as a fraction of the range: the Ping2 resolves 0.5% of what it
# measures, and the high setting is a sonar working in poor conditions.
NOISE_FRACTION: Dict[SonarNoise, float] = {
    SonarNoise.OFF: 0.0,
    SonarNoise.REALISTIC: 0.005,
    SonarNoise.HIGH: 0.02,
}

# The first rangefinder as a real BlueBoat sets it up for its Ping: fed over MAVLink, pointing
# down, good from 20 cm to 100 m. ArduPilot only creates the driver at boot, so turning it on takes
# a restart; turning it off does not, which is what lets the sonar be switched off without one.
MAVLINK_RANGEFINDER = 10.0
RANGEFINDER_PARAMS: Dict[str, float] = {
    "RNGFND1_TYPE": MAVLINK_RANGEFINDER,
    "RNGFND1_ORIENT": 25.0,
    "RNGFND1_MIN": 0.2,
    "RNGFND1_MAX": 100.0,
}
RANGEFINDER_OFF: Dict[str, float] = {"RNGFND1_TYPE": 0.0}

# BlueOS's Ping service sends ten readings a second, and the autopilot drops a MAVLink rangefinder
# that goes half a second without one, so the sounding loop does nothing that can stall it: the
# slow reads (parameters, the vehicle type) run on a loop of their own.
TICK_SECONDS = 0.1
WATCH_SECONDS = 1.0
SPEEDUP_WATCHES = 5
RANGEFINDER_WATCHES = 10

# GLOBAL_POSITION_INT arrives a few times a second, so the position is carried forward along the
# vehicle's velocity between them, for at most this long, rather than sounding the same spot.
EXTRAPOLATE_LIMIT = 1.0

# The survey is a boat's, so it is only sounded under one: a sub would hang its sonar at a depth
# the log knows nothing about, and a rover has no water under it.
BOAT = "Surface Boat"

_task: Optional["asyncio.Task[None]"] = None
_settings: Optional[SonarSettings] = None
_grid: Optional["Grid"] = None
_reading: Optional[SonarReading] = None
_rangefinder_ready: Optional[bool] = None
_receiving: bool = False
_idle_reason: Optional[str] = None
_speedup: float = 1.0


class Grid:
    """A seafloor's depths, sampled where the vehicle is."""

    def __init__(self, meta: Seafloor, cells: "array.array[int]") -> None:
        self.meta = meta
        self.cells = cells
        self.cell_size = meta.cell_size
        self._meters_per_degree_lon = METERS_PER_DEGREE * math.cos(math.radians(meta.origin_latitude))

    def to_local(self, latitude: float, longitude: float) -> Tuple[float, float]:
        """East and north of the origin, in metres."""
        east = (longitude - self.meta.origin_longitude) * self._meters_per_degree_lon
        north = (latitude - self.meta.origin_latitude) * METERS_PER_DEGREE
        return east, north

    def depth(self, row: int, col: int) -> Optional[float]:
        """Depth at a cell centre in metres, or None off the grid or where the survey never went."""
        if not (0 <= row < self.meta.rows and 0 <= col < self.meta.cols):
            return None
        value = self.cells[row * self.meta.cols + col]
        return None if value == NO_DATA else value / 100.0

    def depth_at(self, east: float, north: float) -> Optional[float]:
        """Depth between cell centres, bilinear over whichever of the four corners have data.

        Leaving a missing corner out rather than refusing the point keeps the edge of a survey
        soundable to the last cell it reached instead of half a cell short of it.
        """
        col_f, row_f = east / self.cell_size, north / self.cell_size
        col, row = math.floor(col_f), math.floor(row_f)
        across, up = col_f - col, row_f - row
        total = weight_sum = 0.0
        for d_row, d_col, weight in (
            (0, 0, (1 - across) * (1 - up)),
            (0, 1, across * (1 - up)),
            (1, 0, (1 - across) * up),
            (1, 1, across * up),
        ):
            value = self.depth(row + d_row, col + d_col)
            if value is not None and weight > 0:
                total += value * weight
                weight_sum += weight
        if weight_sum == 0:
            # On a cell centre, or on the line between two, only one corner carries any weight.
            exact = self.depth(round(row_f), round(col_f))
            return exact
        return total / weight_sum


def beam_axis(roll: float, pitch: float, yaw: float) -> Tuple[float, float, float]:
    """Where a downward-looking transducer points, as north, east and down, for a hull's attitude.

    The body's down axis, rotated into the earth frame: the third column of the rotation from
    body to north-east-down for these Euler angles.
    """
    cos_roll, sin_roll = math.cos(roll), math.sin(roll)
    cos_pitch, sin_pitch = math.cos(pitch), math.sin(pitch)
    cos_yaw, sin_yaw = math.cos(yaw), math.sin(yaw)
    return (
        cos_yaw * sin_pitch * cos_roll + sin_yaw * sin_roll,
        sin_yaw * sin_pitch * cos_roll - cos_yaw * sin_roll,
        cos_pitch * cos_roll,
    )


def sound(grid: Grid, east: float, north: float, axis: Tuple[float, float, float]) -> Optional[Tuple[float, float]]:
    """The range a Ping at this spot would report, and how much of its beam found the bottom.

    A Ping answers with its first strong echo, which is the nearest seafloor anywhere in its
    cone rather than the depth straight below: over a slope it reads short, and a hull rolling
    on a swell swings the cone off to one side. None means the beam found no bottom at all.
    """
    axis_north, axis_east, axis_down = axis
    if axis_down < math.cos(math.radians(80)):
        # Rolled or pitched far enough that the beam runs along the surface.
        return None
    below = grid.depth_at(east, north)
    if below is None:
        return None

    # Where the axis meets a seafloor at the depth below, and how wide the cone has spread there.
    reach = below / axis_down
    hit_east, hit_north = east + axis_east * reach, north + axis_north * reach
    on_axis = grid.depth_at(hit_east, hit_north)
    axis_range = (on_axis if on_axis is not None else below) / axis_down
    radius = reach * math.tan(BEAM_HALF_ANGLE) + grid.cell_size

    cell = grid.cell_size
    col_low = max(0, math.floor((hit_east - radius) / cell))
    col_high = min(grid.meta.cols - 1, math.ceil((hit_east + radius) / cell))
    row_low = max(0, math.floor((hit_north - radius) / cell))
    row_high = min(grid.meta.rows - 1, math.ceil((hit_north + radius) / cell))
    span = max(col_high - col_low + 1, row_high - row_low + 1, 1)
    stride = max(1, math.ceil(span / math.sqrt(MAX_BEAM_CELLS)))

    cos_half = math.cos(BEAM_HALF_ANGLE)
    nearest: Optional[float] = None
    in_beam = found = 0
    for row in range(row_low, row_high + 1, stride):
        d_north = row * cell - north
        for col in range(col_low, col_high + 1, stride):
            d_east = col * cell - east
            depth = grid.depth(row, col)
            # A hole in the survey still takes up part of the beam, measured at the depth the axis
            # found, so a sonar half over the edge reports half the confidence.
            d_down = depth if depth is not None else below
            slant = math.sqrt(d_north * d_north + d_east * d_east + d_down * d_down)
            if d_north * axis_north + d_east * axis_east + d_down * axis_down < cos_half * slant:
                continue
            in_beam += 1
            if depth is None:
                continue
            found += 1
            if nearest is None or slant < nearest:
                nearest = slant

    if in_beam == 0:
        # A beam narrower than a cell holds no cell centre, so the bottom along the axis answers.
        return axis_range, 1.0
    if nearest is None:
        return None
    return min(nearest, axis_range), found / in_beam


@dataclass
class Fix:
    """The vehicle's last reported position, and when this service first saw it."""

    counter: int
    seen: float
    latitude: float
    longitude: float
    north_speed: float
    east_speed: float


def extrapolate(fix: Fix, now: float, speedup: float) -> Tuple[float, float]:
    """Where the vehicle has got to since its last fix, carried along its velocity.

    The velocity is in simulated metres per second, so wall-clock time is scaled by SIM_SPEEDUP
    to keep pace with a simulator running faster than real time.
    """
    elapsed = min(max(now - fix.seen, 0.0), EXTRAPOLATE_LIMIT) * speedup
    latitude = fix.latitude + fix.north_speed * elapsed / METERS_PER_DEGREE
    longitude = fix.longitude + fix.east_speed * elapsed / (METERS_PER_DEGREE * math.cos(math.radians(fix.latitude)))
    return latitude, longitude


def measure(range_m: float, coverage: float, noise: SonarNoise) -> Tuple[int, int]:
    """A range and coverage as the Ping would report them: centimetres, and confidence in percent."""
    spread = NOISE_FRACTION[noise] * range_m
    noisy = range_m + (random.gauss(0.0, spread) if spread > 0 else 0.0)
    distance = min(max(round(noisy * 100), MIN_DISTANCE_CM), MAX_DISTANCE_CM)
    confidence = min(max(round(coverage * 100), NO_BOTTOM_CONFIDENCE), 100)
    return distance, confidence


def distance_message(distance_cm: int, confidence: int, time_boot_ms: int) -> Dict[str, Any]:
    """DISTANCE_SENSOR exactly as BlueOS's Ping service sends it, so every reader takes it alike."""
    return {
        "type": "DISTANCE_SENSOR",
        "time_boot_ms": time_boot_ms,
        "min_distance": MIN_DISTANCE_CM,
        "max_distance": MAX_DISTANCE_CM,
        "current_distance": distance_cm,
        "mavtype": {"type": "MAV_DISTANCE_SENSOR_ULTRASOUND"},
        "id": 0,
        "orientation": {"type": "MAV_SENSOR_ROTATION_PITCH_270"},
        "covariance": 255,
        "horizontal_fov": BEAM_WIDTH,
        "vertical_fov": BEAM_WIDTH,
        "quaternion": [0, 0, 0, 0],
        # 0 means "unknown" to MAVLink, which is not what a Ping that lost the bottom says.
        "signal_quality": max(1, confidence),
    }


# ---- The seafloor library -------------------------------------------------------------------


def _decode_grid(upload: SeafloorUpload) -> bytes:
    cells = upload.rows * upload.cols
    if cells > MAX_CELLS:
        raise ValueError(f"A seafloor holds at most {MAX_CELLS} cells; this one has {cells}. Use larger cells.")
    try:
        raw = base64.b64decode(upload.grid, validate=True)
    except binascii.Error as error:
        raise ValueError(f"The seafloor's depth grid is not valid base64: {error}") from error
    if len(raw) != cells * 2:
        raise ValueError(f"A {upload.rows} x {upload.cols} grid takes {cells * 2} bytes, not {len(raw)}.")
    return raw


def _read_cells(meta: Seafloor) -> "array.array[int]":
    cells = array.array("H")
    cells.frombytes(STORE.sidecar(meta.name, GRID_SUFFIX).read_bytes())
    if sys.byteorder == "big":
        cells.byteswap()
    if len(cells) != meta.rows * meta.cols:
        raise ValueError(f"The depth grid of '{meta.name}' does not match its {meta.rows} x {meta.cols} size.")
    return cells


def load_grid(name: str) -> Grid:
    meta = STORE.get(name)
    if meta is None:
        raise UnknownPresetError(f"Unknown seafloor '{name}'.")
    return Grid(meta, _read_cells(meta))


def library() -> List[Seafloor]:
    return STORE.list()


def grid_bytes(name: str) -> bytes:
    if not STORE.has(name):
        raise UnknownPresetError(f"Unknown seafloor '{name}'.")
    return STORE.sidecar(name, GRID_SUFFIX).read_bytes()


def save(upload: SeafloorUpload) -> Seafloor:
    """Store a seafloor, replacing one of the same name, and sound the new one if it is in use."""
    global _grid

    raw = _decode_grid(upload)
    custom_presets.ensure_room(STORE, [], upload.name)
    meta = Seafloor(**upload.dict(exclude={"grid", "builtin", "overridden"}))
    STORE.directory.mkdir(parents=True, exist_ok=True)
    # The grid goes first, so a listed seafloor always has one.
    STORE.sidecar(meta.name, GRID_SUFFIX).write_bytes(raw)
    STORE.save(meta)
    if _sounding(meta.name):
        _grid = load_grid(meta.name)
    return meta


def rename(name: str, new_name: str) -> Seafloor:
    global _settings, _grid

    grid_file = STORE.sidecar(name, GRID_SUFFIX)
    renamed = custom_presets.rename(STORE, STORE.list(), name, new_name)
    if renamed.name != name:
        grid_file.replace(STORE.sidecar(renamed.name, GRID_SUFFIX))
        current = _configured()
        if current.seafloor == name:
            _settings = current.copy(update={"seafloor": renamed.name})
            _store(_settings)
            if _grid is not None:
                _grid = Grid(renamed, _grid.cells)
    return renamed


def remove(name: str) -> str:
    global _settings

    current = _configured()
    if current.seafloor == name and current.enabled:
        raise PresetConflictError(f"The sonar is sounding '{name}'. Switch it off or to another seafloor first.")
    detail = custom_presets.remove(STORE, [], name).replace("preset", "seafloor")
    STORE.sidecar(name, GRID_SUFFIX).unlink(missing_ok=True)
    if current.seafloor == name:
        _settings = current.copy(update={"seafloor": None})
        _store(_settings)
    return detail


# ---- The sonar ------------------------------------------------------------------------------


def _sounding(name: str) -> bool:
    return _task is not None and not _task.done() and _configured().seafloor == name


def _track(fix: Optional[Fix], body: Optional[Dict[str, Any]]) -> Optional[Fix]:
    """The newest position fix, keeping the one before when nothing newer has arrived."""
    if body is None:
        return fix
    message = body.get("message") or {}
    counter = ((body.get("status") or {}).get("time") or {}).get("counter")
    if fix is not None and counter == fix.counter:
        return fix
    latitude, longitude = float(message.get("lat") or 0) / 1e7, float(message.get("lon") or 0) / 1e7
    if latitude == 0 and longitude == 0:
        # No position yet: the autopilot sends zeros until it has one.
        return None
    return Fix(
        counter=counter if isinstance(counter, int) else -1,
        seen=time.monotonic(),
        latitude=latitude,
        longitude=longitude,
        north_speed=float(message.get("vx") or 0) / 100.0,
        east_speed=float(message.get("vy") or 0) / 100.0,
    )


def _attitude(body: Optional[Dict[str, Any]]) -> Tuple[float, float, float]:
    message = (body or {}).get("message") or {}
    return tuple(float(message.get(axis) or 0.0) for axis in ("roll", "pitch", "yaw"))  # type: ignore[return-value]


async def _ping(fix: Optional[Fix], attitude: Tuple[float, float, float], started: float) -> None:
    """Send one sounding for where the vehicle is now."""
    global _reading

    found: Optional[Tuple[float, float]] = None
    latitude = longitude = 0.0
    if fix is not None:
        latitude, longitude = extrapolate(fix, time.monotonic(), _speedup)
        if _grid is not None:
            east, north = _grid.to_local(latitude, longitude)
            found = sound(_grid, east, north, beam_axis(*attitude))

    if found is None:
        distance, confidence = NO_BOTTOM_CM, NO_BOTTOM_CONFIDENCE
    else:
        distance, confidence = measure(found[0], found[1], _configured().noise)
    elapsed_ms = int((time.monotonic() - started) * 1000) % 2**32
    await mavlink.send_message(
        distance_message(distance, confidence, elapsed_ms),
        system_id=DEFAULT_SYSTEM_ID,
        component_id=SONAR_COMPONENT_ID,
    )
    _reading = SonarReading(
        distance=distance / 100.0,
        confidence=confidence,
        bottom=found is not None,
        latitude=latitude,
        longitude=longitude,
    )


async def _sound(active: Dict[str, bool]) -> None:
    started = time.monotonic()
    fix: Optional[Fix] = None
    while True:
        began = time.monotonic()
        if active["boat"]:
            try:
                # Asked together, since every round trip comes out of the same tenth of a second.
                position, attitude = await asyncio.gather(
                    mavlink.read_message("GLOBAL_POSITION_INT"), mavlink.read_message("ATTITUDE")
                )
                fix = _track(fix, position)
                await _ping(fix, _attitude(attitude), started)
            except Exception as error:  # noqa: BLE001 - a missed sounding is not worth ending the loop over
                logger.warning(f"Simulated sounding failed: {error}")
        await asyncio.sleep(max(0.0, TICK_SECONDS - (time.monotonic() - began)))


async def _rangefinder_counter() -> Optional[int]:
    """How many DISTANCE_SENSOR the autopilot itself has sent, which it does only while it has data."""
    body = await mavlink.read_message("DISTANCE_SENSOR")
    counter = (((body or {}).get("status") or {}).get("time") or {}).get("counter")
    return counter if isinstance(counter, int) else None


async def _watch(active: Dict[str, bool]) -> None:
    """The slow checks: what the vehicle is, how fast it runs, and whether the autopilot listens."""
    global _rangefinder_ready, _receiving, _idle_reason, _speedup, _reading

    watch = 0
    counter: Optional[int] = None
    while True:
        try:
            aboard = await autopilot.get_vehicle_type()
            # A vehicle that is not saying, because it is booting, is left as it was.
            if aboard is not None and (aboard == BOAT) != active["boat"]:
                active["boat"] = aboard == BOAT
                _reading = None
                _idle_reason = None if active["boat"] else f"The vehicle is a {aboard}; the sonar only sounds a boat."
            if active["boat"]:
                if watch % SPEEDUP_WATCHES == 0:
                    _speedup = await mavlink.get_param("SIM_SPEEDUP", timeout=0.8) or _speedup
                if watch % RANGEFINDER_WATCHES == 0:
                    kind = await mavlink.get_param("RNGFND1_TYPE", timeout=0.8)
                    if kind is not None:
                        _rangefinder_ready = mavlink.values_match(kind, MAVLINK_RANGEFINDER)
                latest = await _rangefinder_counter()
                _receiving = latest is not None and counter is not None and latest != counter
                counter = latest
        except Exception as error:  # noqa: BLE001 - a missed check is retried on the next one
            logger.warning(f"Simulated sonar check failed: {error}")
        watch += 1
        await asyncio.sleep(WATCH_SECONDS)


async def _run() -> None:
    active = {"boat": True}
    await asyncio.gather(_sound(active), _watch(active))


def _start() -> None:
    global _task, _receiving, _idle_reason
    _receiving = False
    _idle_reason = None
    _task = asyncio.create_task(_run())


async def _stop_task() -> None:
    global _task, _reading
    _reading = None
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None


def _load() -> SonarSettings:
    try:
        return SonarSettings(**json.loads(SONAR_FILE.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return SonarSettings()
    except Exception as error:  # noqa: BLE001 - an unreadable file is a sonar never set up
        logger.warning(f"Ignoring invalid sonar file {SONAR_FILE}: {error}")
        return SonarSettings()


def _store(wanted: SonarSettings) -> None:
    SONAR_FILE.parent.mkdir(parents=True, exist_ok=True)
    SONAR_FILE.write_text(wanted.json(indent=2), encoding="utf-8")


def _configured() -> SonarSettings:
    global _settings
    if _settings is None:
        _settings = _load()
    return _settings


def rangefinder_params() -> Dict[str, float]:
    """What a vehicle configuration has to carry for the autopilot to keep taking the soundings.

    Applying a preset clears every rangefinder it does not name and restarts the autopilot, so
    the apply job writes these along with the preset rather than leaving the sonar to need a
    restart of its own afterwards.
    """
    return dict(RANGEFINDER_PARAMS) if _configured().enabled else {}


def state() -> SonarState:
    running = _task is not None and not _task.done()
    return SonarState(
        settings=_configured(),
        reading=_reading if running else None,
        rangefinder_ready=_rangefinder_ready,
        autopilot_receiving=running and _receiving,
        idle_reason=_idle_reason if running else None,
    )


async def apply(wanted: SonarSettings) -> str:
    """Store the settings and start sounding, or stop and hand the autopilot its rangefinder back.

    Sounding starts before the autopilot is set up for it, so the rangefinder it boots with has
    data from the first moment rather than holding up arming while it waits for some.
    """
    global _settings, _grid, _rangefinder_ready

    grid: Optional[Grid] = None
    if wanted.enabled:
        if not wanted.seafloor:
            raise PresetConflictError("Choose a seafloor for the sonar to sound.")
        grid = load_grid(wanted.seafloor)

    await _stop_task()
    _settings = wanted
    _store(wanted)
    _grid = grid

    if not wanted.enabled:
        _rangefinder_ready = None
        # Confirmed, because a rangefinder left on with nothing feeding it stops the vehicle arming.
        _, unverified = await mavlink.set_params_verified(RANGEFINDER_OFF, attempts=2)
        if unverified:
            return "The sonar is off, but RNGFND1_TYPE did not take 0, so the vehicle may refuse to arm for want of soundings."
        return "The sonar is off, and so is the autopilot's rangefinder."

    _start()
    before = await mavlink.get_param("RNGFND1_TYPE", attempts=2)
    _, unverified = await mavlink.set_params_verified(RANGEFINDER_PARAMS, attempts=2)
    if "RNGFND1_TYPE" in unverified:
        raise RuntimeError("The autopilot did not take RNGFND1_TYPE, so it cannot read the sonar.")
    if before is not None and mavlink.values_match(before, MAVLINK_RANGEFINDER):
        _rangefinder_ready = True
        return f"Sounding '{wanted.seafloor}'."

    await autopilot.restart()
    if not await mavlink.wait_until_ready(timeout=VEHICLE_READY_TIMEOUT):
        return (
            f"Sounding '{wanted.seafloor}', but the autopilot did not come back from the restart that "
            "makes it read the sonar in time to confirm it."
        )
    _rangefinder_ready = True
    return f"Sounding '{wanted.seafloor}'. The autopilot restarted to take the sonar as its rangefinder."


async def resume() -> None:
    """Take the sonar back up when the service starts.

    The autopilot is still set up for a rangefinder only this service feeds, so a sonar that was
    sounding has to start again, even over a seafloor that has gone missing, or the vehicle
    refuses to arm for want of its data.
    """
    global _grid

    wanted = _configured()
    if not wanted.enabled:
        return
    try:
        _grid = load_grid(wanted.seafloor) if wanted.seafloor else None
    except Exception as error:  # noqa: BLE001 - sounding nothing still keeps the vehicle armable
        logger.warning(f"Could not load seafloor '{wanted.seafloor}': {error}")
        _grid = None
    _start()
    logger.info(f"Sounding seafloor '{wanted.seafloor}'")


async def shutdown() -> None:
    await _stop_task()


if __name__ == "__main__":
    # The model is worth a check that runs: a sonar reading the wrong cell still looks plausible.

    def _grid_of(rows: int, cols: int, depths: List[int], cell: float = 1.0) -> Grid:
        meta = Seafloor(
            name="test",
            origin_latitude=0.0,
            origin_longitude=0.0,
            cell_size=cell,
            rows=rows,
            cols=cols,
            depth_min=0.0,
            depth_max=0.0,
        )
        return Grid(meta, array.array("H", depths))

    level = math.radians(0)
    down = beam_axis(level, level, level)
    assert all(abs(a - b) < 1e-9 for a, b in zip(down, (0.0, 0.0, 1.0))), down

    # Rolling to starboard swings the beam to port, and the heading turns that with the hull.
    rolled = beam_axis(math.radians(10), 0.0, 0.0)
    assert rolled[1] < 0 and abs(rolled[0]) < 1e-9, rolled
    rolled_east = beam_axis(math.radians(10), 0.0, math.radians(90))
    assert rolled_east[0] > 0 and abs(rolled_east[1]) < 1e-9, rolled_east

    # Bilinear between four cells, and a missing corner left out rather than read as zero.
    square = _grid_of(2, 2, [1000, 2000, 1000, 2000])
    assert abs((square.depth_at(0.5, 0.5) or 0) - 15.0) < 1e-9
    holed = _grid_of(2, 2, [1000, NO_DATA, 1000, NO_DATA])
    assert abs((holed.depth_at(0.5, 0.5) or 0) - 10.0) < 1e-9
    assert _grid_of(1, 1, [NO_DATA]).depth_at(0.0, 0.0) is None
    assert square.depth_at(-5.0, -5.0) is None

    # A flat bottom 10 m down reads 10 m straight below, with the whole beam on it.
    flat = _grid_of(41, 41, [1000] * 41 * 41)
    flat_range, flat_coverage = sound(flat, 20.0, 20.0, down) or (0.0, 0.0)
    assert abs(flat_range - 10.0) < 1e-6 and flat_coverage == 1.0, (flat_range, flat_coverage)

    # A shoal inside the cone is what the echo comes back from, not the bottom straight below.
    shoal = [1000] * 41 * 41
    shoal[20 * 41 + 21] = 600
    shoal_range, _ = sound(_grid_of(41, 41, shoal), 20.0, 20.0, down) or (0.0, 0.0)
    assert abs(shoal_range - math.sqrt(36 + 1)) < 1e-6, shoal_range
    # One outside the cone is not heard at all.
    beside = [1000] * 41 * 41
    beside[20 * 41 + 25] = 600
    beside_range, _ = sound(_grid_of(41, 41, beside), 20.0, 20.0, down) or (0.0, 0.0)
    assert abs(beside_range - 10.0) < 1e-6, beside_range

    # Half the beam over the edge of the survey finds the bottom with half the confidence.
    half = [1000 if col <= 20 else NO_DATA for _ in range(41) for col in range(41)]
    _, half_coverage = sound(_grid_of(41, 41, half), 20.0, 20.0, down) or (0.0, 0.0)
    assert 0.4 < half_coverage < 0.75, half_coverage
    # And off the grid there is nothing to find.
    assert sound(flat, 500.0, 500.0, down) is None

    # A fix is carried along its velocity, faster under SIM_SPEEDUP, and only so far.
    moving = Fix(counter=1, seen=100.0, latitude=0.0, longitude=0.0, north_speed=1.0, east_speed=0.0)
    lat_half, _ = extrapolate(moving, 100.5, 1.0)
    assert abs(lat_half * METERS_PER_DEGREE - 0.5) < 1e-6
    lat_fast, _ = extrapolate(moving, 100.5, 4.0)
    assert abs(lat_fast * METERS_PER_DEGREE - 2.0) < 1e-6
    lat_stale, _ = extrapolate(moving, 160.0, 1.0)
    assert abs(lat_stale * METERS_PER_DEGREE - EXTRAPOLATE_LIMIT) < 1e-6

    # Readings stay inside what the Ping can report.
    assert measure(500.0, 1.0, SonarNoise.OFF) == (MAX_DISTANCE_CM, 100)
    assert measure(0.01, 0.0, SonarNoise.OFF) == (MIN_DISTANCE_CM, NO_BOTTOM_CONFIDENCE)
    assert measure(12.345, 0.5, SonarNoise.OFF) in ((1234, 50), (1235, 50))

    print("seafloor model checks pass")
