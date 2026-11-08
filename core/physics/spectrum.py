"""日出 / 日落直射光透射谱与"预计霞光颜色"。

本模块只依赖 Python 标准库 ``math``，用一套可解释的物理链路估算太阳贴近
地平线时 400–700 nm 直射光的颜色。与"把光线一路算到地面"的常见做法不同，
霞光照亮的是**云底**，因此光深必须从云层高度起算：光线只需穿过云层上方
那一段大气就能抵达太空，而不必继续穿过整层稠密低层大气。

1. **光深起点在云层高度**：云层本身位于高度 ``h``（高云 8600 m、中云
   5200 m、低云 1500 m）。阳光从太空射向云底，穿过的是云层以上的大气。
   若仍用 Kasten & Young 的平面平行空气质量把光算到地面，太阳高度 0° 时
   空气质量约 37.9、550 nm 透射率只剩约 0.024，日落瞬间会被错误地压成
   接近全黑；而且太阳落到地平线以下后完全无法表达"高云仍被点亮"这一
   火烧云的核心事实。因此本模块改用弯曲大气的 Chapman 近似。

2. **瑞利与气溶胶分别积分**：大气并非整层均匀。分子（瑞利）标高约
   ``H_R = 8000 m``，而气溶胶集中在边界层，标高只有 ``H_A = 1500 m``。
   对云层高度 ``h`` 处的目标，沿前向射线分别积分每种成分的等效柱质量

   ``m_X = ∫ exp(-z / H_X) ds / H_X``

   其中 ``z`` 为海拔、``ds`` 为沿射线的路径元，``H_X`` 为该成分的标高。
   两者绝不能共用一个 Chapman 空气质量：Chapman 式只对高度尺度与
   ``R/2 ≈ 3200 km`` 可比的整层等效柱成立，而 1.5 km 以下的气溶胶在
   8600 m 高云的光路里几乎不存在；若让 AOD 乘上整层 Chapman 质量，会把
   气溶胶凭空放大一个量级，把日落错误地压黑。:func:`slant_airmass` 仅保留
   作快速粗估，不再参与主链路。

3. **受光极限（地球本影）**：当太阳低到云层落入地球本影后，云底不再有
   直射阳光。由切线几何 ``r cosα = R`` 得到

   ``α_min(h) = -sqrt(2h/R)``（弧度，等价于负的度数）

   1500 m 约 -1.24°，5200 m 约 -2.31°，8600 m 约 -2.98°。低于该极限时
   直射光谱为 0；这正是高云在日落后仍能保持暖色、而低云更早熄灭的
   物理原因。真实太阳有约 0.266° 的视半直径，本影边缘是渐变的，因此透射
   谱再乘以 ``smoothstep(α_min - 0.266°, α_min + 0.266°, α)`` 的软边因子，
   避免亮度在本影边缘硬跳变。

4. **瑞利散射**：洁净大气的垂直光学厚度在 550 nm 处取
   ``τ_R,550 = 0.098``，波长依赖取 ``τ_R(λ) = τ_R,550 * (λ / 550)^-4.1``。
   蓝光被强烈散射，低太阳时只剩红橙光，这是霞光变红的根本原因。

5. **气溶胶米散射**：Ångström 幂律
   ``τ_A(λ) = AOD550 * (λ / 550)^-1.3``（Ångström 指数约 1.3）。
   本步骤尚未接入 CAMS 气溶胶，AOD550 固定为中性假设值 0.2。

6. **透射率**：``T(λ) = exp(-(τ_R(λ)·m_R + τ_A(λ)·m_A))``，其中 ``m_R`` 与
   ``m_A`` 分别用瑞利 / 气溶胶标高沿球面几何积分得到。若射线撞地（``m`` 为
   无穷大），返回全 0；否则再乘以上述受光软边因子。

7. **颜色**：把 ``T(λ)`` 乘以太阳光谱权重（缺省为等能谱），再用
   Wyman, Sloan & Shirley (2013) 的多瓣高斯解析近似计算 CIE 1931
   ``x̄(λ), ȳ(λ), z̄(λ)``，得到 XYZ，最后直接经 CIE XYZ → linear sRGB →
   sRGB gamma 编码。不再"按 Y=1 归一化取色度再乘亮度"——那样会在低太阳时
   抽走光谱自身的明暗关系并灰化暖色。``Y`` 只用于计算 CIE ``L*`` 感知亮度
   与色名，不参与色度混色。

云量不参与光谱计算，只用来判断"有没有一块可以被染色的画布"，以及哪一层
云仍处在受光范围内（高云优先）。三层云量都很低时没有可承接的云幕；所有
云层都进入本影后亮度自然归零。

文献
----
* Chapman, S. (1931). The absorption and dissociative or ionizing effect of
  monochromatic radiation in an atmosphere on a rotating earth.
  *Proceedings of the Physical Society*, 43(1), 26–45.
* Kasten, F. & Young, A. T. (1989). Revised optical air mass tables and
  approximation formula. *Applied Optics*, 28(22), 4735–4738.
* Wyman, C., Sloan, P.-P. & Shirley, P. (2013). Simple Analytic
  Approximations to the CIE XYZ Color Matching Functions. *Journal of
  Computer Graphics Techniques*, 2(2), 1–11.
* Ångström, A. (1929). On the atmospheric transmission of sun radiation and
  on dust in the air. *Geografiska Annaler*, 11, 156–166.
"""

from __future__ import annotations

import math

__all__ = [
    "WAVELENGTHS",
    "TAU_R_550",
    "RAYLEIGH_EXPONENT",
    "ANGSTROM_EXPONENT",
    "DEFAULT_AOD550",
    "R_EARTH",
    "H_RAYLEIGH_M",
    "H_AEROSOL_M",
    "ATM_TOP_M",
    "SUN_ANGULAR_RADIUS_DEG",
    "DECK_HEIGHT_M",
    "shadow_limit_deg",
    "slant_airmass",
    "column_airmass",
    "transmittance",
    "spectrum_to_xyz",
    "xyz_to_srgb",
    "canvas_strength",
    "lit_deck",
    "deck_light",
    "colour_name",
]

# 400..700 nm，每 10 nm 一个样本（含端点，共 31 个）。
WAVELENGTHS = tuple(range(400, 701, 10))

TAU_R_550 = 0.098       # 550 nm 垂直瑞利光学厚度（干洁大气）
RAYLEIGH_EXPONENT = 4.1  # 瑞利散射的波长幂律指数
ANGSTROM_EXPONENT = 1.3  # 气溶胶 Ångström 指数
DEFAULT_AOD550 = 0.2     # 本阶段假设的中性 AOD550

R_EARTH = 6371000.0      # 地球半径（m）
H_RAYLEIGH_M = 8000.0    # 瑞利（分子）标高
H_AEROSOL_M = 1500.0     # 气溶胶标高（边界层为主）
ATM_TOP_M = 120000.0     # 大气顶（积分上限）
SUN_ANGULAR_RADIUS_DEG = 0.266  # 太阳视半直径
DECK_HEIGHT_M = {        # 与 burning_cloud.DECKS 的气候值一致
    "high": 8600.0,
    "mid": 5200.0,
    "low": 1500.0,
}

_DECK_NAMES = (("high", "高云"), ("mid", "中云"), ("low", "低云"))


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _smoothstep(edge0: float, edge1: float, x: float) -> float:
    if edge1 == edge0:
        return 0.0 if x < edge0 else 1.0
    t = _clamp((x - edge0) / (edge1 - edge0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def shadow_limit_deg(deck_height_m: float) -> float:
    """云层仍被太阳直射的最低太阳高度角（度，负值）。

    由地球本影几何 ``sin²α + 2h/R = 0`` 得到 ``α_min = -sqrt(2h/R)``。
    高度越低，云层越早进入本影，极限越接近 0°。
    """
    height = max(0.0, float(deck_height_m))
    return -math.degrees(math.sqrt(2.0 * height / R_EARTH))


def slant_airmass(elevation_deg: float, deck_height_m: float) -> float:
    """Chapman 式快速粗估空气质量。

    ``m(α, h) = 1 / sqrt(sin²α + 2h/R)``。α=90° 时约 1；在受光极限以上随
    α 降低而增大。

    .. note::
       这只是 Chapman 近似，把整个大气当作单一高度尺度的柱；它无法区分
       瑞利（8 km）与气溶胶（1.5 km）的标高差异，也不适用于精确的掠射
       光路。主链路请使用 :func:`column_airmass` 分别积分两种成分。
    """
    height = max(0.0, float(deck_height_m))
    sine = math.sin(math.radians(float(elevation_deg)))
    return 1.0 / math.sqrt(sine * sine + 2.0 * height / R_EARTH)


def _trapezoid(func, t_max: float, samples: int) -> float:
    """在 ``[0, t_max]`` 上对 ``func(t)`` 做梯形积分。"""
    if t_max <= 0.0:
        return 0.0
    count = max(1, int(samples))
    step = t_max / count
    total = 0.0
    previous = func(0.0)
    for index in range(1, count + 1):
        current = func(index * step)
        total += 0.5 * (previous + current) * step
        previous = current
    return total


def column_airmass(elevation_deg: float, deck_height_m: float,
                   scale_height_m: float, *, samples: int = 96) -> float:
    """沿前向射线精确积分球面几何的等效柱质量（以标高为单位）。

    对指数大气 ``ρ(z) ∝ exp(-z / H)``，沿射线的等效垂直柱质量为

    ``m = ∫ exp(-z / H) ds / H``

    ``z`` 为海拔，``ds`` 为路径元。目标在云层高度 ``h0``，射线与地心的
    撞击参数 ``p = (R + h0) cosα``；若 ``sinα < 0`` 且 ``p < R``，射线会
    撞到地面，返回 ``math.inf``。

    积分用 ``h = hp + t²`` 替换消除掠射处的 ``1/sqrt`` 奇点：向上射线取
    ``hp = h0``，从 ``h0`` 积到大气顶；向下射线取切点高度 ``hp = p - R``，
    先积 ``hp → ATM_TOP_M``，再补回 ``hp → h0`` 切点以下那一段。分段在
    ``t`` 上做梯形积分（不是固定步长沿 ``s``，否则掠射处需要数百万步）。
    """
    elevation = float(elevation_deg)
    height = max(0.0, float(deck_height_m))
    scale_height = float(scale_height_m)
    if scale_height <= 0.0:
        raise ValueError("scale_height_m 必须为正")

    angle = math.radians(elevation)
    sine = math.sin(angle)
    cosine = math.cos(angle)
    impact = (R_EARTH + height) * cosine
    if sine < 0.0 and impact < R_EARTH:
        return math.inf

    if sine >= 0.0:
        tangent_height = height
        radius0 = R_EARTH + height
        sine_sq = sine * sine
        top = math.sqrt(max(ATM_TOP_M - tangent_height, 0.0))

        def integrand(t: float) -> float:
            altitude = tangent_height + t * t
            radius = R_EARTH + altitude
            if t == 0.0:
                if sine_sq == 0.0:
                    density = math.exp(-altitude / scale_height)
                    return density * math.sqrt(2.0 * radius0) / scale_height
                return 0.0
            denominator = math.sqrt(
                radius0 * radius0 * sine_sq
                + 2.0 * radius0 * t * t
                + t ** 4
            )
            density = math.exp(-altitude / scale_height)
            return density * 2.0 * radius * t / denominator / scale_height

        return _trapezoid(integrand, top, samples)

    tangent_height = impact - R_EARTH
    tangent_radius = R_EARTH + tangent_height
    top = math.sqrt(max(ATM_TOP_M - tangent_height, 0.0))
    denominator_const = 2.0 * tangent_radius

    def integrand(t: float) -> float:
        altitude = tangent_height + t * t
        radius = R_EARTH + altitude
        density = math.exp(-altitude / scale_height)
        return (
            density * 2.0 * radius
            / math.sqrt(denominator_const + t * t)
            / scale_height
        )

    upward = _trapezoid(integrand, top, samples)
    below_tangent = math.sqrt(max(height - tangent_height, 0.0))
    return upward + _trapezoid(integrand, below_tangent, samples)


def transmittance(elevation_deg: float, aod550: float = DEFAULT_AOD550,
                  deck_height_m: float = DECK_HEIGHT_M["high"]) -> list:
    """返回 400–700 nm（每 10 nm）云底处透射率列表（31 个值）。

    公式::

        T(λ) = exp(-(τ_R(λ)·m_R + τ_A(λ)·m_A)) · fade

        τ_R(λ) = 0.098 * (λ / 550)^-4.1
        τ_A(λ) = AOD550 * (λ / 550)^-1.3

    ``m_R`` 与 ``m_A`` 分别由 :func:`column_airmass` 用瑞利 / 气溶胶标高从
    云层高度起算；``fade`` 为受光极限处的太阳软边因子。若射线撞地，返回
    31 个 0.0。
    """
    elevation = float(elevation_deg)
    height = max(0.0, float(deck_height_m))
    aod = max(0.0, float(aod550))

    mass_rayleigh = column_airmass(elevation, height, H_RAYLEIGH_M)
    mass_aerosol = column_airmass(elevation, height, H_AEROSOL_M)
    if math.isinf(mass_rayleigh) or math.isinf(mass_aerosol):
        return [0.0] * len(WAVELENGTHS)

    limit = shadow_limit_deg(height)
    fade = _smoothstep(
        limit - SUN_ANGULAR_RADIUS_DEG,
        limit + SUN_ANGULAR_RADIUS_DEG,
        elevation,
    )

    result = []
    for wavelength in WAVELENGTHS:
        ratio = wavelength / 550.0
        tau_rayleigh = TAU_R_550 * ratio ** (-RAYLEIGH_EXPONENT)
        tau_aerosol = aod * ratio ** (-ANGSTROM_EXPONENT)
        optical_depth = (
            tau_rayleigh * mass_rayleigh + tau_aerosol * mass_aerosol
        )
        result.append(math.exp(-optical_depth) * fade)
    return result


def _gaussian_lobe(wavelength: float, mu: float, sigma_left: float,
                   sigma_right: float) -> float:
    """Wyman et al. (2013) 的非对称高斯瓣。"""
    sigma = sigma_left if wavelength < mu else sigma_right
    return math.exp(-0.5 * ((wavelength - mu) / sigma) ** 2)


def _cie_xyz_bar(wavelength: float):
    """CIE 1931 配色函数的多瓣高斯解析近似（Wyman 2013）。"""
    x_bar = (
        1.056 * _gaussian_lobe(wavelength, 599.8, 37.9, 31.0)
        + 0.362 * _gaussian_lobe(wavelength, 442.0, 16.0, 26.7)
        - 0.065 * _gaussian_lobe(wavelength, 501.1, 20.4, 26.2)
    )
    y_bar = (
        0.821 * _gaussian_lobe(wavelength, 568.8, 46.9, 40.5)
        + 0.286 * _gaussian_lobe(wavelength, 530.9, 16.3, 31.1)
    )
    z_bar = (
        1.217 * _gaussian_lobe(wavelength, 437.0, 11.8, 36.0)
        + 0.681 * _gaussian_lobe(wavelength, 459.0, 26.0, 13.8)
    )
    return x_bar, y_bar, z_bar


def spectrum_to_xyz(transmittance, solar_irradiance=None):
    """透射谱 → CIE XYZ。

    *transmittance* 为 400–700 nm 每 10 nm 的 31 个透射率；可选
    *solar_irradiance* 为同长度的太阳光谱权重。缺省使用等能谱（全 1），
    足以保留"低太阳变红"这一相对色度信息。

    返回的 XYZ 已按无大气衰减的同一太阳谱白点归一化：无衰减时 Y≈1，
    因此 Y 可直接当作相对亮度使用。
    """
    values = list(transmittance)
    if len(values) != len(WAVELENGTHS):
        raise ValueError(
            "transmittance 需要 %d 个样本，实际 %d 个"
            % (len(WAVELENGTHS), len(values))
        )
    if solar_irradiance is None:
        weights = [1.0] * len(WAVELENGTHS)
    else:
        weights = list(solar_irradiance)
        if len(weights) != len(WAVELENGTHS):
            raise ValueError(
                "solar_irradiance 需要 %d 个样本，实际 %d 个"
                % (len(WAVELENGTHS), len(weights))
            )

    big_x = big_y = big_z = white_y = 0.0
    for wavelength, value, weight in zip(WAVELENGTHS, values, weights):
        spd = max(0.0, float(weight)) * _clamp(float(value), 0.0, 1.0)
        x_bar, y_bar, z_bar = _cie_xyz_bar(wavelength)
        big_x += spd * x_bar
        big_y += spd * y_bar
        big_z += spd * z_bar
        white_y += max(0.0, float(weight)) * y_bar

    if white_y <= 1e-12:
        return 0.0, 0.0, 0.0
    return big_x / white_y, big_y / white_y, big_z / white_y


def _gamma_encode(channel: float) -> float:
    """linear sRGB → sRGB gamma 编码。"""
    if channel <= 0.0031308:
        return 12.92 * channel
    return 1.055 * channel ** (1.0 / 2.4) - 0.055


def xyz_to_srgb(X: float, Y: float, Z: float):
    """CIE XYZ → sRGB（0–255，含 gamma 编码与 gamut 裁剪）。

    使用标准 sRGB / D65 矩阵；超出 [0, 1] 的线性通道直接裁剪到色域内。
    """
    x = float(X)
    y = float(Y)
    z = float(Z)
    if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
        return 0, 0, 0
    if x <= 0.0 and y <= 0.0 and z <= 0.0:
        return 0, 0, 0

    linear_r = 3.2406 * x - 1.5372 * y - 0.4986 * z
    linear_g = -0.9689 * x + 1.8758 * y + 0.0415 * z
    linear_b = 0.0557 * x - 0.2040 * y + 1.0570 * z

    channels = []
    for channel in (linear_r, linear_g, linear_b):
        channel = _clamp(channel, 0.0, 1.0)
        channels.append(int(round(_gamma_encode(channel) * 255.0)))
    return channels[0], channels[1], channels[2]


def canvas_strength(cloud_low: float = 0.0, cloud_mid: float = 0.0,
                    cloud_high: float = 0.0) -> float:
    """云幕作为"可染色画布"的存在度（0–1）。

    只回答"有没有画布"，不做任何光谱计算。三层云量都低于 5% 时明确返回
    0；高云是火烧云的主要画布，中云可作次级画布，厚低云会遮住画布。
    """
    low = _clamp(float(cloud_low), 0.0, 100.0)
    mid = _clamp(float(cloud_mid), 0.0, 100.0)
    high = _clamp(float(cloud_high), 0.0, 100.0)
    if low < 5.0 and mid < 5.0 and high < 5.0:
        return 0.0
    high_canvas = _smoothstep(4.0, 40.0, high)
    mid_canvas = 0.75 * _smoothstep(4.0, 35.0, mid)
    veil = 1.0 - 0.55 * _smoothstep(55.0, 100.0, low)
    return _clamp(max(high_canvas, mid_canvas) * veil, 0.0, 1.0)


def lit_deck(sun_elevation_deg: float, cloud_low: float = 0.0,
             cloud_mid: float = 0.0, cloud_high: float = 0.0):
    """返回第一个仍被太阳直射的云层 ``(中文名, 高度 m)``。

    按 high → mid → low 的顺序检查，云量需 >= 10%，且太阳高度角必须高于
    该云层的 :func:`shadow_limit_deg`。全部不满足时返回 ``(None, None)``。
    """
    elevation = float(sun_elevation_deg)
    coverage = {
        "high": float(cloud_high),
        "mid": float(cloud_mid),
        "low": float(cloud_low),
    }
    for key, label in _DECK_NAMES:
        height = DECK_HEIGHT_M[key]
        if coverage[key] >= 10.0 and elevation > shadow_limit_deg(height):
            return label, height
    return None, None


def _rgb_to_hsl(r: float, g: float, b: float):
    """RGB 0–255 → HSL（H 0–360°，S/L 0–1）。"""
    red, green, blue = r / 255.0, g / 255.0, b / 255.0
    high = max(red, green, blue)
    low = min(red, green, blue)
    lightness = (high + low) / 2.0
    delta = high - low
    if delta <= 1e-12:
        return 0.0, 0.0, lightness
    saturation = delta / (1.0 - abs(2.0 * lightness - 1.0)) if lightness not in (0.0, 1.0) else 0.0
    if high == red:
        hue = 60.0 * (((green - blue) / delta) % 6.0)
    elif high == green:
        hue = 60.0 * (((blue - red) / delta) + 2.0)
    else:
        hue = 60.0 * (((red - green) / delta) + 4.0)
    return hue % 360.0, _clamp(saturation, 0.0, 1.0), lightness


def colour_name(r: float, g: float, b: float, brightness: float) -> str:
    """把 RGB + 亮度映射为中文色名。

    判定依据（HSL 色相区间 + 亮度档位）：

    * ``S < 0.12`` 或 ``brightness < 0.03``：低饱和/近黑。色相 190–260°
      → **灰蓝**，260–340° → **灰紫**，其余 → **中性灰**。
    * 色相 0–8°：暖红端，``brightness < 0.30`` → **暗红**，否则 **玫红**。
    * 色相 8–20°：``brightness < 0.22`` → **暗红**，否则 **橙红**。
    * 色相 20–30°：``brightness < 0.30`` → **橙红**，否则 **橙金**。
    * 色相 30–45°：``brightness < 0.72`` → **橙金**，否则 **金黄**。
    * 色相 45–68°：**金黄**（亮度不足时退为 **橙金**）。
    * 色相 250–330° 的紫端：低饱和或低亮度 → **灰紫**，否则 **玫红**。
    * 色相 330–360°：``brightness < 0.25`` → **暗红**，否则 **玫红**。

    *brightness* 为 :func:`deck_light` 输出的 0–1 感知亮度。
    """
    red = _clamp(float(r), 0.0, 255.0)
    green = _clamp(float(g), 0.0, 255.0)
    blue = _clamp(float(b), 0.0, 255.0)
    light = _clamp(float(brightness), 0.0, 1.0)
    hue, saturation, _ = _rgb_to_hsl(red, green, blue)

    if saturation < 0.12 or light < 0.03:
        if light < 0.03:
            return "中性灰"
        if 190.0 <= hue <= 260.0 or (blue > red + 4.0 and blue >= green):
            return "灰蓝"
        if 260.0 < hue <= 340.0:
            return "灰紫"
        return "中性灰"

    if 190.0 <= hue < 260.0 and saturation < 0.55:
        return "灰蓝"
    if 160.0 <= hue < 190.0:
        return "灰蓝"

    if 250.0 <= hue < 330.0:
        if saturation < 0.50 or light < 0.30:
            return "灰紫"
        return "玫红"
    if 330.0 <= hue < 345.0:
        return "玫红" if light >= 0.22 else "暗红"
    if 345.0 <= hue or hue < 8.0:
        return "暗红" if light < 0.30 else "玫红"
    if hue < 20.0:
        return "暗红" if light < 0.22 else "橙红"
    if hue < 30.0:
        return "橙红" if light < 0.30 else "橙金"
    if hue < 45.0:
        return "橙金" if light < 0.72 else "金黄"
    if hue < 68.0:
        return "金黄" if light >= 0.45 else "橙金"
    return "金黄" if light >= 0.45 else "橙金"


def deck_light(elevation_deg: float, aod550: float = DEFAULT_AOD550,
               *, cloud_low: float = 0.0, cloud_mid: float = 0.0,
               cloud_high: float = 0.0, canvas=None) -> dict:
    """主入口：估算日出 / 日落时云幕的预计霞光颜色。

    参数
    ----
    elevation_deg:
        太阳高度角（度）。是否还有光由各云层的受光极限决定；太阳低于
        -12° 时所有云层必然已进入本影，亮度自然为 0。
    aod550:
        550 nm 气溶胶光学厚度。本步骤固定中性假设值 0.2，输出中
        ``aod_source`` 标记为 ``"assumed"``。
    cloud_low / cloud_mid / cloud_high:
        低 / 中 / 高云量（%）。用于 :func:`lit_deck` 选择受光云层，并交给
        :func:`canvas_strength` 估计画布强度；不改变透射光谱本身。
    canvas:
        可直接指定画布存在度（0–1），用于测试或上游另有画布判断时覆盖
        云量推导。

    返回 ``{"hex", "name", "brightness", "rgb", "aod_source", "deck",
    "deck_height_m"}``：``hex`` 为 ``#rrggbb``，``brightness`` 为 0–1
    两位小数，``rgb`` 为 0–255 整数列表。没有受光云层时返回黑色、中性灰、
    亮度 0，``deck`` 与 ``deck_height_m`` 均为 ``None``。
    """
    elevation = float(elevation_deg)
    aod = max(0.0, float(aod550))

    deck_name, deck_height = lit_deck(
        elevation, cloud_low=cloud_low, cloud_mid=cloud_mid,
        cloud_high=cloud_high,
    )
    if deck_name is None:
        return {
            "hex": "#000000",
            "name": "中性灰",
            "brightness": 0.0,
            "rgb": [0, 0, 0],
            "aod_source": "assumed",
            "deck": None,
            "deck_height_m": None,
        }

    spectrum = transmittance(elevation, aod, deck_height)
    big_x, big_y, big_z = spectrum_to_xyz(spectrum)

    # 直接由 XYZ 转 sRGB：保留光谱自身的明暗与色度，避免灰化暖色。
    rgb_linear = xyz_to_srgb(big_x, big_y, big_z)

    if canvas is None:
        canvas = canvas_strength(cloud_low, cloud_mid, cloud_high)
    canvas = _clamp(float(canvas), 0.0, 1.0)

    # CIE L* 作为感知亮度，再由画布强度调制。Y 已按无大气太阳谱归一化。
    luma = max(big_y, 0.0)
    if luma > 0.008856:
        lightness = 116.0 * luma ** (1.0 / 3.0) - 16.0
    else:
        lightness = 903.3 * luma
    brightness = _clamp(canvas * lightness / 100.0, 0.0, 1.0)

    # 仅在画布很弱时向中性灰蓝收拢；canvas >= 0.35 完全不混灰。
    if canvas < 0.35:
        weight = (0.35 - canvas) / 0.35
    else:
        weight = 0.0
    neutral = (118.0, 128.0, 146.0)
    red = rgb_linear[0] * (1.0 - weight) + neutral[0] * brightness * weight
    green = rgb_linear[1] * (1.0 - weight) + neutral[1] * brightness * weight
    blue = rgb_linear[2] * (1.0 - weight) + neutral[2] * brightness * weight
    rgb = [int(_clamp(round(value), 0, 255)) for value in (red, green, blue)]

    brightness = round(brightness, 2)
    name = colour_name(rgb[0], rgb[1], rgb[2], brightness)
    hex_value = "#%02x%02x%02x" % (rgb[0], rgb[1], rgb[2])

    return {
        "hex": hex_value,
        "name": name,
        "brightness": brightness,
        "rgb": rgb,
        "aod_source": "assumed",
        "deck": deck_name,
        "deck_height_m": deck_height,
    }
