"""Sea of Clouds Index (SCI) model.

Analyses a vertical pressure-level profile (temperature / dew point / relative
humidity / height) to locate:

* the temperature **inversion layer** that caps a cloud deck,
* the **condensation level** (cloud base) and the top of the saturated layer,

then compares those heights with an observation spot's DEM elevation to
decide the viewing state:

* ``俯瞰云海`` - the spot sits above the cloud deck and looks down on it,
* ``云中雾中`` - the spot is inside the cloud deck,
* ``云层在上方`` - clouds are overhead (spot below the deck),
* ``晴空无云`` - no organised cloud deck / inversion.

The 0-100 SCI rewards a strong, moist inversion with the spot positioned a
short distance above the cloud top and dry, clear air above it.

Only the Python standard library is used.
"""

from __future__ import annotations

import math

__all__ = [
    "compute_sci",
    "sea_potential",
    "overlook_state",
    "detect_inversion",
    "condensation_level",
    "relative_humidity",
    "saturation_dewpoint",
    "STATUS_OVERLOOK",
    "STATUS_IN_CLOUD",
    "STATUS_BELOW",
    "STATUS_CLEAR",
    "STATE_OVERLOOK",
    "STATE_IN_CLOUD",
    "STATE_CLEAR",
    "IDEAL_MARGIN_MIN",
    "IDEAL_MARGIN_MAX",
    "MOUNTAIN_MIN_ELEVATION",
    "VIEWPOINT_MARGIN_MIN",
    "MIN_INVERSION_STRENGTH",
    "DRY_ABOVE_MAX",
    "VISIBILITY_MIN_KM",
    "DEWPOINT_SPREAD_MIN",
    "is_mountain_viewpoint",
]

STATUS_OVERLOOK = "俯瞰云海"
STATUS_IN_CLOUD = "云中雾中"
STATUS_BELOW = "云层在上方"
STATUS_CLEAR = "晴空无云"

# Machine-readable viewing states used by the grid / frontend.
STATE_OVERLOOK = "overlook_sea"
STATE_IN_CLOUD = "inside_cloud"
STATE_CLEAR = "clear_sky"

_STATUS_TO_STATE = {
    STATUS_OVERLOOK: STATE_OVERLOOK,
    STATUS_IN_CLOUD: STATE_IN_CLOUD,
    STATUS_BELOW: STATE_CLEAR,
    STATUS_CLEAR: STATE_CLEAR,
}


def overlook_state(status: str) -> str:
    """Map a Chinese SCI status to its machine-readable viewing state."""
    return _STATUS_TO_STATE.get(status, STATE_CLEAR)

# Saturation / cloud detection thresholds.
_SATURATED_RH = 90.0
_SATURATED_SPREAD = 0.5

# Weights for the "overlook the sea of clouds" score (weighted geometric mean).
_W_INVERSION = 0.35
_W_MOISTURE = 0.25
_W_MARGIN = 0.20
_W_DRY = 0.20

_FLOOR = 0.03

# 逆温硬门控：没有逆温或逆温太弱时只是普通低云/雾，不能算云海。
MIN_INVERSION_STRENGTH = 0.5
# 青龙 SEA_DRY_ABOVE_MAX：云顶之上平均 RH 达到该值说明整层饱和/阴天，
# 没有干空气托底的"海面"，判无云海。
DRY_ABOVE_MAX = 88.0

# 青龙沉淀：真正的云海要求观景山峰海拔高出逆温层（云顶）150~600 m。
# 云顶几乎贴着脚下（<150 m）看不到"海"；高出太多（>600 m）则云海被稀释，
# 观感下降。平原/市区点位天然低于云顶，永远落在"云层在上方"而不得分。
IDEAL_MARGIN_MIN = 150.0
IDEAL_MARGIN_MAX = 600.0
# 高山点位海拔下限（米）：低于此高度视为平原/城区，不参与云海观海判定。
MOUNTAIN_MIN_ELEVATION = 200.0
# 对齐青龙 SEA_MARGIN_MIN：云顶距山顶不足 30 m（或高于山顶）时，
# 人在云里/云上，不能算俯瞰云海。
VIEWPOINT_MARGIN_MIN = 30.0
# 对齐青龙硬闸门：能见度低于 3km 说明人在雾中，不能判俯瞰云海。
VISIBILITY_MIN_KM = 3.0
# 对齐青龙硬闸门：地面露点差低于 1.5℃ 说明近饱和/雾，不能判俯瞰云海。
DEWPOINT_SPREAD_MIN = 1.5


def is_mountain_viewpoint(elevation_m: float) -> bool:
    """Return ``True`` when *elevation_m* is high enough to overlook a deck."""
    try:
        return float(elevation_m) >= MOUNTAIN_MIN_ELEVATION
    except (TypeError, ValueError):
        return False


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _smoothstep(edge0: float, edge1: float, x: float) -> float:
    if edge1 == edge0:
        return 0.0 if x < edge0 else 1.0
    t = _clamp((x - edge0) / (edge1 - edge0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _margin_score(margin_m: float) -> float:
    """Score the viewpoint-above-cloud-top margin (青龙 150~600 m 窗口).

    * below ~150 m the deck is looming at the viewer's feet (or enveloping
      it) — a real "sea" cannot be seen;
    * 150-600 m is the sweet spot for looking *down* on a rolling sea;
    * far above (hundreds of metres to kilometres) the sea flattens out and
      scores progressively lower.
    """
    margin = _clamp(margin_m, 0.0, 20000.0)
    rise = _smoothstep(30.0, IDEAL_MARGIN_MIN, margin)
    fade = 1.0 - 0.6 * _smoothstep(IDEAL_MARGIN_MAX, 1500.0, margin)
    return _clamp(rise * fade, _FLOOR, 1.0)


def _get(level, *names):
    """Read the first present field from a dict-like or object-like level."""
    if isinstance(level, dict):
        for name in names:
            if name in level and level[name] is not None:
                return level[name]
        return None
    for name in names:
        value = getattr(level, name, None)
        if value is not None:
            return value
    return None


def saturation_dewpoint(temperature_c: float, relative_humidity_pct: float) -> float:
    """Dew point (degC) from temperature and relative humidity (Magnus)."""
    rh = _clamp(relative_humidity_pct, 1.0, 100.0) / 100.0
    gamma = math.log(rh) + (17.67 * temperature_c) / (temperature_c + 243.5)
    return (243.5 * gamma) / (17.67 - gamma)


def relative_humidity(temperature_c: float, dewpoint_c: float) -> float:
    """Relative humidity (%) from temperature and dew point (Magnus)."""

    def _es(t: float) -> float:
        return 6.112 * math.exp((17.67 * t) / (t + 243.5))

    return _clamp(100.0 * _es(dewpoint_c) / _es(temperature_c), 0.0, 100.0)


def _normalise_profile(profile) -> list:
    levels = []
    for raw in profile:
        height = _get(raw, "height", "elevation", "geopotential_height", "h")
        temperature = _get(raw, "temperature", "temp", "t")
        if height is None or temperature is None:
            continue
        dewpoint = _get(raw, "dewpoint", "dew_point", "td")
        rh = _get(raw, "relative_humidity", "humidity", "rh")
        if dewpoint is None and rh is not None:
            dewpoint = saturation_dewpoint(temperature, rh)
        if rh is None and dewpoint is not None:
            rh = relative_humidity(temperature, dewpoint)
        levels.append(
            {
                "height": float(height),
                "temperature": float(temperature),
                "dewpoint": float(dewpoint) if dewpoint is not None else None,
                "rh": float(rh) if rh is not None else None,
            }
        )
    levels.sort(key=lambda item: item["height"])
    return levels


def condensation_level(levels) -> float | None:
    """Estimate the lifting condensation level (metres) from the surface layer."""
    if not levels:
        return None
    surface = levels[0]
    spread = _get(surface, "temperature") - _get(surface, "dewpoint")
    return surface["height"] + 125.0 * max(0.0, spread)


def detect_inversion(levels) -> dict | None:
    """Return the strongest temperature inversion in the profile.

    ``{"base", "top", "strength"}`` in metres / degC, or ``None``.
    """
    best = None
    for lower, upper in zip(levels, levels[1:]):
        dz = upper["height"] - lower["height"]
        if dz <= 0:
            continue
        d_temp = upper["temperature"] - lower["temperature"]
        if d_temp > 0.0:
            if best is None or d_temp > best["strength"]:
                best = {
                    "base": lower["height"],
                    "top": upper["height"],
                    "strength": d_temp,
                }
    return best


def _cloud_layer(levels, viewpoint_elevation: float | None = None) -> dict | None:
    """Return the highest continuous saturated deck below the viewpoint.

    连续饱和段之间只要夹着一个不饱和层就断开，不能把地面湿层和高空湿层
    粘成一片假云。传入 ``viewpoint_elevation`` 时，云顶贴近或高于山顶的段
    一律丢弃（人在云里/云上，不是看海），再取剩余段中云顶最高的一段。
    """
    segments = []
    current = []
    for level in levels:
        saturated = (
            level["rh"] is not None and level["rh"] >= _SATURATED_RH
        ) or (
            level["dewpoint"] is not None
            and level["temperature"] - level["dewpoint"] <= _SATURATED_SPREAD
        )
        if saturated:
            current.append(level)
        elif current:
            segments.append(current)
            current = []
    if current:
        segments.append(current)

    if viewpoint_elevation is not None:
        segments = [
            segment
            for segment in segments
            if segment[-1]["height"] < viewpoint_elevation - VIEWPOINT_MARGIN_MIN
        ]
    if not segments:
        return None

    chosen = max(segments, key=lambda segment: segment[-1]["height"])
    base = chosen[0]["height"]
    top = chosen[-1]["height"]
    rh_values = [level["rh"] for level in chosen if level["rh"] is not None]
    return {
        "base": base,
        "top": top,
        "mean_rh": sum(rh_values) / len(rh_values) if rh_values else 95.0,
    }


def _mean_rh_above(levels, height: float) -> float | None:
    values = [
        level["rh"] for level in levels if level["height"] > height and level["rh"] is not None
    ]
    return sum(values) / len(values) if values else None


def _overlook_score(inversion_strength: float, mean_rh: float, margin: float, rh_above):
    inv = _clamp(inversion_strength / 5.0, _FLOOR, 1.0)
    moisture = _clamp((mean_rh - 70.0) / 30.0, _FLOOR, 1.0)
    margin_score = _margin_score(margin)
    if rh_above is None:
        dry = 0.6
    else:
        dry = _clamp((95.0 - rh_above) / 45.0, _FLOOR, 1.0)

    score = 100.0 * (
        inv**_W_INVERSION
        * moisture**_W_MOISTURE
        * margin_score**_W_MARGIN
        * dry**_W_DRY
    )
    return _clamp(score, 0.0, 100.0), {
        "inversion": round(inv, 3),
        "moisture": round(moisture, 3),
        "margin": round(margin_score, 3),
        "dry_above": round(dry, 3),
    }


def compute_sci(
    profile,
    viewpoint_elevation: float,
    wind_speed: float | None = None,
    *,
    visibility_km: float | None = None,
    dewpoint_spread: float | None = None,
) -> dict:
    """Compute the Sea of Clouds Index for a spot at *viewpoint_elevation*.

    Parameters
    ----------
    profile:
        Iterable of pressure levels. Each level is a mapping (or object) with
        ``height`` (m), ``temperature`` (degC) and either ``dewpoint`` or
        ``relative_humidity`` (%). ``pressure`` is optional but reported.
    viewpoint_elevation:
        Spot DEM elevation in metres.
    wind_speed:
        Optional surface wind (m/s). Very strong wind raises a small penalty.
    visibility_km:
        Optional surface visibility in kilometres. Below ``VISIBILITY_MIN_KM``
        the spot is treated as inside fog and never scored as an overlook.
    dewpoint_spread:
        Optional surface dew-point spread in degC. Below
        ``DEWPOINT_SPREAD_MIN`` the surface is near saturation / foggy and
        never scored as an overlook.

    Returns
    -------
    dict with ``sci`` (0-100), ``status``, ``inversion_height``,
    ``cloud_base``, ``cloud_top``, ``inversion_strength`` and ``components``.
    """
    levels = _normalise_profile(profile)
    inversion = detect_inversion(levels)
    cloud = _cloud_layer(levels, viewpoint_elevation)
    # 没有低于观景点的候选云层时，仍用完整廓线判断"云中/云下"，
    # 但绝不据此判"俯瞰云海"，保持 IN_CLOUD / BELOW 的既有行为。
    overlook_allowed = cloud is not None
    if cloud is None:
        cloud = _cloud_layer(levels)
    lcl = condensation_level(levels)

    inversion_strength = inversion["strength"] if inversion else 0.0
    inversion_height = inversion["base"] if inversion else None
    rh_above = _mean_rh_above(levels, cloud["top"]) if cloud else None

    components = {}
    if cloud is None:
        status = STATUS_CLEAR
        sci = 5.0
        components["note"] = "无逆温、无饱和层"
    elif inversion is None or inversion_strength < MIN_INVERSION_STRENGTH:
        status = STATUS_CLEAR
        sci = 5.0
        components["note"] = "无封顶逆温"
    elif rh_above is not None and rh_above >= DRY_ABOVE_MAX:
        status = STATUS_CLEAR
        sci = 5.0
        components["note"] = "云顶之上整层饱和"
    elif (
        overlook_allowed
        and viewpoint_elevation >= cloud["top"]
        and (
            (visibility_km is not None and visibility_km < VISIBILITY_MIN_KM)
            or (dewpoint_spread is not None and dewpoint_spread < DEWPOINT_SPREAD_MIN)
        )
    ):
        # 雾中/近饱和时人其实在云里，硬闸门压掉"俯瞰云海"的假阳。
        status = STATUS_IN_CLOUD
        sci = 5.0
        if visibility_km is not None and visibility_km < VISIBILITY_MIN_KM:
            components["note"] = "雾中能见度不足"
        else:
            components["note"] = "近饱和露点差不足"
    elif overlook_allowed and viewpoint_elevation >= cloud["top"]:
        status = STATUS_OVERLOOK
        margin = viewpoint_elevation - cloud["top"]
        sci, components = _overlook_score(
            inversion_strength,
            cloud["mean_rh"],
            margin,
            rh_above,
        )
        components["margin_m"] = round(margin, 1)
    elif viewpoint_elevation >= cloud["base"]:
        status = STATUS_IN_CLOUD
        thickness = cloud["top"] - cloud["base"]
        thickness_score = _clamp(thickness / 1000.0, 0.0, 1.0)
        moisture = _clamp((cloud["mean_rh"] - 80.0) / 20.0, 0.0, 1.0)
        sci = _clamp(12.0 + 13.0 * moisture + 10.0 * thickness_score, 10.0, 35.0)
        components = {
            "moisture": round(moisture, 3),
            "thickness": round(thickness_score, 3),
        }
    else:
        status = STATUS_BELOW
        moisture = _clamp((cloud["mean_rh"] - 80.0) / 20.0, 0.0, 1.0)
        sci = _clamp(5.0 + 10.0 * moisture, 5.0, 20.0)
        components = {"moisture": round(moisture, 3)}

    # 高山 / 平原城区严格区分：低于山地高度门槛的点位只看头顶，
    # 永不判"俯瞰云海"（平原市区绝不出云海指数）。
    mountain_viewpoint = is_mountain_viewpoint(viewpoint_elevation)
    components["mountain_viewpoint"] = mountain_viewpoint
    if not mountain_viewpoint:
        if status == STATUS_OVERLOOK:
            status = STATUS_BELOW
            components["note"] = "平原/城区点位，不判云海"
        sci = min(sci, 15.0)

    if wind_speed is not None and wind_speed > 12.0:
        sci = _clamp(sci * (1.0 - min(0.3, (wind_speed - 12.0) / 60.0)), 0.0, 100.0)
        components["wind_speed"] = round(float(wind_speed), 1)

    return {
        "sci": round(sci, 1),
        "status": status,
        "state": overlook_state(status),
        "inversion_height": inversion_height,
        "inversion_strength": round(inversion_strength, 2),
        "cloud_base": cloud["base"] if cloud else None,
        "cloud_top": cloud["top"] if cloud else None,
        "condensation_level": round(lcl, 1) if lcl is not None else None,
        "components": components,
    }


# Weights for the terrain-independent cloud-sea *potential* field.
_W_POT_INVERSION = 0.40
_W_POT_MOISTURE = 0.35
_W_POT_THICKNESS = 0.25


def sea_potential(profile) -> dict:
    """Terrain-independent "will a cloud sea form here" potential (0-100).

    Unlike :func:`compute_sci` this does not need a viewpoint elevation: it
    scores the raw profile for a moist low-level saturated deck capped by a
    temperature inversion. It powers the map's 云海潜在发生场 (SCI) layer,
    where the actual on-the-ground overlook state depends on local terrain.

    Returns ``potential`` (0-100), ``inversion_probability`` (0-1),
    ``inversion_height``, ``inversion_strength``, ``cloud_base`` and
    ``cloud_top``.
    """
    levels = _normalise_profile(profile)
    inversion = detect_inversion(levels)
    cloud = _cloud_layer(levels)

    inversion_strength = inversion["strength"] if inversion else 0.0
    inversion_height = inversion["base"] if inversion else None

    if cloud is None:
        potential = 3.0
        components = {"note": "无饱和层"}
    else:
        inv = _clamp(inversion_strength / 5.0, _FLOOR, 1.0)
        moisture = _clamp((cloud["mean_rh"] - 75.0) / 25.0, _FLOOR, 1.0)
        thickness = _clamp((cloud["top"] - cloud["base"]) / 800.0, _FLOOR, 1.0)
        if inversion is None:
            inv = max(_FLOOR, inv * 0.35)
        potential = 100.0 * (
            inv**_W_POT_INVERSION
            * moisture**_W_POT_MOISTURE
            * thickness**_W_POT_THICKNESS
        )
        components = {
            "inversion": round(inv, 3),
            "moisture": round(moisture, 3),
            "thickness": round(thickness, 3),
        }

    potential = _clamp(potential, 0.0, 100.0)
    return {
        "potential": round(potential, 1),
        "inversion_probability": round(potential / 100.0, 3),
        "inversion_height": inversion_height,
        "inversion_strength": round(inversion_strength, 2),
        "cloud_base": cloud["base"] if cloud else None,
        "cloud_top": cloud["top"] if cloud else None,
        "components": components,
    }
