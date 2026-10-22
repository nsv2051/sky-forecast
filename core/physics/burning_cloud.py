"""Burning Cloud Index (BCI) model.

The BCI estimates how favourable the sky is for a "burning cloud" (火烧云)
display around sunrise/sunset.  The model is a faithful port of the algorithms
that were validated over many seasons in the QingLong (青龙) / LOHUI monitoring
stack, plus a long-range light-path obstruction check.

Physical reasoning (硬物理判据)
------------------------------

* **A burning cloud needs a translucent mid/high "canvas"**.  Broken cirrus
  (``cloud_high`` roughly 30–75%) with a moderate mid deck catches grazing
  light and glows best.
* **Thick mid cloud is a grey board that blocks the light** — when
  ``cloud_mid > 70`` and the high deck is weak (``cloud_high < 50``) the sky is
  a dead, flat grey and the index is crushed below 30.
* **An ultra-thick high deck is a white screen** — when ``cloud_high > 85``
  (LOHUI screenshots) sunlight cannot transmit at all and the deck reads as
  leaden / dead-white; the index is strongly suppressed.
* **Low cloud near the sun's projection path veils the whole thing** — the
  grazing light has to cross the low deck, so a thick/cumuliform low layer
  progressively swallows the display.  Thin fragments (<=35%) are harmless,
  a broken deck (35-75%) only lets a shrinking fraction of the light through,
  and a solid overcast (>=75%) seals the corridor.  This obstruction is modelled
  as a continuous transmission curve (:func:`low_cloud_transmission`) so the
  spatial field feathers organically instead of being cut by a hard gate.
* **Long-range light path** — at sunrise/sunset the sun is near the horizon, so
  the light must travel hundreds of kilometres through the lower atmosphere.
  Thick low/mid cloud 100–500 km away along the sun azimuth severs that
  corridor even when the deck overhead looks perfect.

Everything is pure Python (standard library only) and returns a 0-100 index
plus the component breakdown, so it can be unit tested without any network.
"""

from __future__ import annotations

import math

__all__ = [
    "compute_bci",
    "grade_bci",
    "light_path_severity",
    "apply_light_path_penalty",
    "low_cloud_transmission",
    "cloud_canvas",
    "BURNING_CLOUD_MIN",
    "BURNING_CLOUD_MAX",
    "LOW_CLOUD_CLEAR",
    "LOW_CLOUD_SEALED",
    "LOW_CLOUD_FLOOR",
    "MID_GREY_BOARD",
    "MID_GREY_HIGH_WEAK",
    "HIGH_WHITE_SCREEN",
    "GREY_BOARD_CAP",
    "WHITE_SCREEN_CAP",
    "PATH_BLOCK_MID",
    "PATH_BLOCK_LOW",
    "PATH_BLOCK_RH",
    "PATH_BLOCK_FRAC",
]

BURNING_CLOUD_MIN = 0.0
BURNING_CLOUD_MAX = 100.0

# --- 画布（高云 / 中云）模型参数 -------------------------------------------
# 高云（卷云/卷层云）是主要透光画布，58% 左右最通透；中云（高积云）峰值更低、
# 且只能作为次要画布。所有常量均为云量百分比或无量纲系数。
_CANVAS_HIGH_PEAK = 58.0  # 高云最佳透光覆盖率 (%)
_CANVAS_HIGH_WIDTH = 38.0  # 高云高斯画布的宽度 (%)
_CANVAS_MID_PEAK = 30.0  # 中云最佳承接掠射光的覆盖率 (%)
_CANVAS_MID_WIDTH = 32.0  # 中云高斯画布的宽度 (%)
_CANVAS_CEIL = 0.92  # 画布质量上限（留一点余量，避免满分幻觉）
_CANVAS_MID_ONLY_FLOOR = 0.55  # 无高云时中云画布的折扣（中云独撑只是灰板）
_CANVAS_HIGH_SUPPORT = 40.0  # 高云达到该覆盖率时中云画布按满值计入 (%)

# --- 低云遮蔽：平滑透射曲线（替代 50% 硬阶跃刀锋） -------------------------
# 低云不是开关，而是一层逐渐变厚的纱幕：薄碎低云几乎不挡光，随着低云
# 覆盖率上升，射向中高云画布的掠射光被逐段吞没，最终被厚低云完全封死。
#   cloud_low <= 35%            -> 透射率 1.0（无遮挡）
#   35% <  cloud_low < 75%      -> smoothstep 平滑羽化衰减
#   cloud_low >= 75%            -> 压制入底（<= 0.05，视线封死）
LOW_CLOUD_CLEAR = 35.0  # cloud_cover_low (%) below which the deck is transparent
LOW_CLOUD_SEALED = 75.0  # cloud_cover_low (%) above which the corridor is sealed
LOW_CLOUD_FLOOR = 0.04  # residual transmission when fully sealed (below 0.05)

# --- 青龙沉淀：厚中云死灰板 / 超厚高云死白幕布 -----------------------------
MID_GREY_BOARD = 70.0  # cloud_mid above this is a grey, light-blocking board
MID_GREY_HIGH_WEAK = 50.0  # ... but only when the high deck is weak
HIGH_WHITE_SCREEN = 85.0  # cloud_high above this is an opaque dead-white screen
GREY_BOARD_CAP = 30.0  # mid grey-board caps the index below 30
WHITE_SCREEN_CAP = 45.0  # ultra-thick high deck caps the index
TRANSLUCENT_LOW = 30.0  # ideal translucent cirrus band (lower bound)
TRANSLUCENT_HIGH = 75.0  # ideal translucent cirrus band (upper bound)

# --- 长程进光廊道（100~500 km 截面） --------------------------------------
PATH_BLOCK_MID = 70.0  # a mid-cloud section above this blocks the corridor
PATH_BLOCK_LOW = 50.0  # a low-cloud section above this (with high RH) blocks
PATH_BLOCK_RH = 80.0  # relative humidity gate for the low-cloud block
PATH_BLOCK_FRAC = 0.5  # fraction of blocked sections that severs the corridor

# Weights sum to 1.0 and are applied as a weighted geometric mean. Horizon and
# sun are no longer weighted components: they multiply the final score as
# physical gates (no grazing light -> no burning cloud at all).
_WEIGHT_CLOUD = 0.55
_WEIGHT_VISIBILITY = 0.25
_WEIGHT_AQI = 0.20

_FLOOR = 0.01


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _smoothstep(edge0: float, edge1: float, x: float) -> float:
    if edge1 == edge0:
        return 0.0 if x < edge0 else 1.0
    t = _clamp((x - edge0) / (edge1 - edge0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def low_cloud_transmission(cloud_low: float) -> float:
    """Fraction of grazing light that survives the low-level deck (0.04-1.0).

    A physical, C1-continuous veil instead of a hard ``cloud_low > 50`` cliff:

    * ``cloud_low <= 35``  -> ``1.0`` (thin fragments cast no useful shadow);
    * ``35 < cloud_low < 75`` -> ``1.0 - smoothstep(35, 75, cloud_low)``,
      feathered down so the spatial field transitions naturally;
    * ``cloud_low >= 75``  -> sealed, pinned at :data:`LOW_CLOUD_FLOOR`
      (below 0.05, i.e. the grazing light never reaches the mid/high canvas).
    """
    low = _clamp(cloud_low, 0.0, 100.0)
    if low <= LOW_CLOUD_CLEAR:
        return 1.0
    if low >= LOW_CLOUD_SEALED:
        return LOW_CLOUD_FLOOR
    veil = _smoothstep(LOW_CLOUD_CLEAR, LOW_CLOUD_SEALED, low)
    return LOW_CLOUD_FLOOR + (1.0 - LOW_CLOUD_FLOOR) * (1.0 - veil)


def cloud_canvas(cloud_mid: float, cloud_high: float) -> float:
    """中高云作为火烧云"画布"的质量（0-1）。

    高云（卷云/卷层云）是主要透光画布，峰值在 58% 附近；超过 85% 后逐渐变成
    不透光的死白幕布，用平滑衰减而不是硬截断来压制。中云（高积云）自身也能
    承接掠射光，但峰值更低（30%），0% 时不得白拿分数；中云越厚，挡在后方高云
    画布前的灰幕越重。没有高云时，中云独撑只能算一块更暗的灰板，因此再按
    高云的存在度打折。
    """
    mid = _clamp(cloud_mid, 0.0, 100.0)
    high = _clamp(cloud_high, 0.0, 100.0)

    q_high = _CANVAS_CEIL * math.exp(
        -(((high - _CANVAS_HIGH_PEAK) / _CANVAS_HIGH_WIDTH) ** 2)
    )
    q_high *= 1.0 - 0.75 * _smoothstep(85.0, 118.0, high)

    presence = _smoothstep(3.0, 18.0, mid)
    q_mid = _CANVAS_CEIL * math.exp(
        -(((mid - _CANVAS_MID_PEAK) / _CANVAS_MID_WIDTH) ** 2)
    )
    q_mid *= (1.0 - 0.85 * _smoothstep(45.0, 82.0, mid)) * presence
    high_support = _CANVAS_MID_ONLY_FLOOR + (
        1.0 - _CANVAS_MID_ONLY_FLOOR
    ) * _smoothstep(0.0, _CANVAS_HIGH_SUPPORT, high)
    q_mid *= high_support

    mid_veil = 1.0 - 0.80 * _smoothstep(30.0, 85.0, mid)

    return max(_FLOOR, max(q_high * mid_veil, q_mid))


def _horizon_score(low_cloud_pct: float, horizon_low_cloud_pct: float | None) -> float:
    """远端低云对进光廊道的遮挡系数（0.05-1.0）。

    只惩罚"远端"低云：当远端输入缺失、或与本地低云完全相同时，它只是
    ``cloud_low`` 的复写，本地低云已由 :func:`low_cloud_transmission` 处理，
    这里再扣一次属于重复惩罚，直接返回 1.0。
    """
    if horizon_low_cloud_pct is None:
        return 1.0
    if abs(float(horizon_low_cloud_pct) - float(low_cloud_pct)) < 1e-9:
        return 1.0
    local = _clamp(low_cloud_pct, 0.0, 100.0)
    remote = _clamp(horizon_low_cloud_pct, 0.0, 100.0)
    weighted = 0.4 * local + 0.6 * remote
    return _clamp(1.0 - (weighted / 100.0) * 0.95, 0.05, 1.0)


def _visibility_score(visibility_km: float) -> float:
    return _smoothstep(2.0, 30.0, visibility_km)


def _aqi_score(aqi: float) -> float:
    return _clamp(1.0 - (aqi - 30.0) / 170.0, 0.4, 1.0)


def _sun_score(sun_elevation_deg: float) -> float:
    """太阳高度角对火烧云的乘性闸门（0.03-1.0）。

    只有贴近地平线的掠射光才能烧起来：太阳落到地平线以下越深，可用的
    余晖越少；太阳升高后光线变白、角度变陡，火烧云迅速熄灭。该分数直接
    乘在最终指数上，因此没有掠射光时可以一票否决。
    """
    e = sun_elevation_deg
    if e <= -2.0:
        return 0.03 + 0.97 * _smoothstep(-12.0, -2.0, e)
    if e <= 6.0:
        return 1.0
    return 1.0 - 0.95 * _smoothstep(6.0, 26.0, e)


def grade_bci(bci: float) -> str:
    """Human readable grade for a BCI value."""
    if bci >= 75.0:
        return "极佳"
    if bci >= 55.0:
        return "良好"
    if bci >= 35.0:
        return "一般"
    return "较差"


def _section_severity(low, mid, rh) -> float:
    """Obstruction severity (0-1) of a single light-path section."""
    sev = 0.0
    if mid is not None and mid > PATH_BLOCK_MID:
        sev = max(sev, (mid - PATH_BLOCK_MID) / (100.0 - PATH_BLOCK_MID))
    if low is not None and low > PATH_BLOCK_LOW:
        if rh is None or rh >= PATH_BLOCK_RH:
            sev = max(sev, (low - PATH_BLOCK_LOW) / (100.0 - PATH_BLOCK_LOW))
    return _clamp(sev, 0.0, 1.0)


def light_path_severity(samples) -> dict:
    """Assess a long-range light-path corridor from sampled sections.

    *samples* is an iterable of mappings/objects with ``low`` (and optionally
    ``mid`` / ``rh``) cloud cover percentages at successive distances (typically
    100/200/350/500 km along the sun azimuth).  Returns ``{"blocked",
    "severity", "blocked_fraction", "sections"}``.

    This ports the QingLong corridor check: >=50% of sections blocked severs the
    corridor; severity is the larger of the mean severity and the blocked
    fraction, so near-total obstruction reads close to 1.0.
    """
    details = []
    for sample in samples or []:
        if isinstance(sample, dict):
            low = sample.get("low")
            mid = sample.get("mid")
            rh = sample.get("rh", sample.get("relative_humidity"))
        else:
            low = getattr(sample, "low", None)
            mid = getattr(sample, "mid", None)
            rh = getattr(sample, "rh", None)
        details.append(_section_severity(low, mid, rh))
    if not details:
        return {"blocked": False, "severity": 0.0, "blocked_fraction": 0.0, "sections": 0}
    blocked_n = sum(1 for sev in details if sev >= 0.5)
    frac = blocked_n / float(len(details))
    mean_sev = sum(details) / float(len(details))
    blocked = frac >= PATH_BLOCK_FRAC
    severity = max(mean_sev, frac if blocked else 0.0)
    return {
        "blocked": blocked,
        "severity": round(_clamp(severity, 0.0, 1.0), 3),
        "blocked_fraction": round(frac, 3),
        "sections": len(details),
    }


def apply_light_path_penalty(score: float, path_info) -> float:
    """Suppress a burning-cloud score by the corridor obstruction severity.

    A clear corridor is untouched.  A blocked corridor (>=50% sections) is
    crushed below 25, and a near-total block (>=75%) below 15, so we never
    advertise a "false burn" when the incoming light is severed.

    入参与返回值均为 0-100 量纲。
    """
    if not path_info or not path_info.get("blocked"):
        return score
    sev = float(path_info.get("severity") or 0.0)
    out = score * (1.0 - 0.7 * sev)
    if sev >= 0.75:
        out = min(out, 15.0)
    elif sev >= 0.5:
        out = min(out, 25.0)
    return _clamp(out, 0.0, 100.0)


def compute_bci(
    cloud_low: float,
    cloud_mid: float,
    cloud_high: float,
    visibility_km: float = 20.0,
    aqi: float | None = None,
    sun_elevation: float = -1.0,
    sun_azimuth: float | None = None,
    horizon_low_cloud: float | None = None,
    light_path=None,
    humidity: float | None = None,
) -> dict:
    """Compute the Burning Cloud Index.

    Parameters
    ----------
    cloud_low, cloud_mid, cloud_high:
        Cloud cover percentages (0-100) for the low/mid/high layers.
    visibility_km:
        Ground-level visibility in kilometres (QWeather).
    aqi:
        Air quality index. ``None`` disables the aerosol penalty.
    sun_elevation:
        Solar elevation in degrees (see :mod:`core.physics.solar`).
    sun_azimuth:
        Solar azimuth in degrees (kept for callers / diagnostics).
    horizon_low_cloud:
        Low-cloud fraction along the sun projection path. Defaults to
        ``cloud_low`` when omitted.
    light_path:
        Optional long-range corridor assessment from
        :func:`light_path_severity`. When blocked, the final score is crushed.
    humidity:
        Optional 2 m relative humidity (%), used for the transparency
        confidence correction (thick + humid = rain-stratus, blocked).

    Returns
    -------
    dict with ``bci`` (0-100), ``grade``, ``gate``, ``flags`` and
    ``components``.
    """
    if horizon_low_cloud is None:
        horizon_low_cloud = cloud_low

    cloud_mid = _clamp(cloud_mid, 0.0, 100.0)
    cloud_high = _clamp(cloud_high, 0.0, 100.0)
    cloud_low = _clamp(cloud_low, 0.0, 100.0)

    flags = []
    grey_board = cloud_mid > MID_GREY_BOARD and cloud_high < MID_GREY_HIGH_WEAK
    white_screen = cloud_high > HIGH_WHITE_SCREEN
    if grey_board:
        flags.append("mid_grey_board")
    if white_screen:
        flags.append("high_white_screen")

    # --- 平滑物理闸门 ---------------------------------------------------
    # 没有中/高云画布 -> 完全没有火烧云。用 smoothstep 平滑起势，避免
    # "14% -> 16%" 时从 0 突跳到几十的悬崖；完全无云时仍返回全零。
    mid_high_total = cloud_mid + cloud_high
    if mid_high_total <= 0.0:
        return {
            "bci": 0.0,
            "grade": grade_bci(0.0),
            "gate": "no_mid_high_cloud",
            "flags": flags,
            "components": {
                "cloud": 0.0,
                "horizon": 0.0,
                "visibility": 0.0,
                "aqi": 0.0,
                "sun": 0.0,
            },
        }
    gate_factor = _smoothstep(0.0, 50.0, mid_high_total)

    # --- Transparency confidence (厚湿云再打折) -------------------------
    # 阈值用平滑过渡而不是硬切换，避免扫描云量时出现 >15 分的单步跳变。
    transparency = 1.0
    humidity_value = humidity if humidity is not None else None
    if humidity_value is not None and humidity_value >= 75.0:
        # 高湿厚中云：典型雨层云/层积云，55% 起逐渐压到 0.5。
        transparency = 1.0 - 0.5 * _smoothstep(55.0, 70.0, cloud_mid)
    elif cloud_mid >= 70.0:
        transparency = 1.0 - 0.4 * _smoothstep(55.0, 80.0, cloud_mid)
    elif cloud_mid >= 55.0 and cloud_high >= 70.0:
        # 中高云同厚，透光也差。
        transparency = 1.0 - 0.25 * _smoothstep(50.0, 70.0, cloud_mid)
    else:
        transparency = 1.0 - 0.4 * _smoothstep(55.0, 80.0, cloud_mid)

    # 低云纱幕：掠射光穿过低云层后剩余的比例，连续羽化中高云画布的亮度。
    low_transmission = low_cloud_transmission(cloud_low)
    comp_cloud = max(
        _FLOOR,
        cloud_canvas(cloud_mid, cloud_high) * transparency * low_transmission,
    )
    comp_horizon = max(_FLOOR, _horizon_score(cloud_low, horizon_low_cloud))
    comp_visibility = max(_FLOOR, _visibility_score(visibility_km))
    comp_sun = _sun_score(sun_elevation)
    comp_aqi = (
        max(_FLOOR, _aqi_score(float(aqi))) if aqi is not None else 1.0
    )

    bci = 100.0 * (
        comp_cloud**_WEIGHT_CLOUD
        * comp_visibility**_WEIGHT_VISIBILITY
        * comp_aqi**_WEIGHT_AQI
    )
    # 太阳高度角与远端遮挡是乘性闸门：没有掠射光 / 进光廊道被截断时，
    # 几何平均里的权重再高也救不回来。
    bci *= comp_sun * gate_factor * comp_horizon

    # --- 青龙沉淀软压：死灰板 / 死白幕布 -------------------------------
    grey_ramp = _smoothstep(MID_GREY_BOARD, 100.0, cloud_mid)
    if grey_board:
        bci *= 1.0 - 0.6 * grey_ramp
        soft_cap = BURNING_CLOUD_MAX - (
            BURNING_CLOUD_MAX - GREY_BOARD_CAP
        ) * grey_ramp
        bci = min(bci, soft_cap)
    if white_screen:
        # 透射衰减拉长到 85-118%，与 cloud_canvas 的白幕布处理一致，避免
        # 85% 附近出现单步 >15 分的硬拐点；上限则仍平滑收敛到 WHITE_SCREEN_CAP。
        white_fade = _smoothstep(HIGH_WHITE_SCREEN, 118.0, cloud_high)
        bci *= 1.0 - 0.7 * white_fade
        white_ramp = _smoothstep(HIGH_WHITE_SCREEN, 100.0, cloud_high)
        soft_cap = BURNING_CLOUD_MAX - (
            BURNING_CLOUD_MAX - WHITE_SCREEN_CAP
        ) * white_ramp
        bci = min(bci, soft_cap)

    # --- 长程进光廊道遮挡：强力压制 ------------------------------------
    if light_path:
        bci = apply_light_path_penalty(bci, light_path)
        if light_path.get("blocked"):
            flags.append("light_path_blocked")

    bci = _clamp(bci, BURNING_CLOUD_MIN, BURNING_CLOUD_MAX)

    return {
        "bci": round(bci, 1),
        "grade": grade_bci(bci),
        "gate": None,
        "flags": flags,
        "components": {
            "cloud": round(comp_cloud, 3),
            "horizon": round(comp_horizon, 3),
            "visibility": round(comp_visibility, 3),
            "aqi": round(comp_aqi, 3),
            "sun": round(comp_sun, 3),
        },
    }
