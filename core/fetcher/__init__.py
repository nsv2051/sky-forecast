"""Weather data fetchers (Open-Meteo, QWeather) with offline fallbacks."""

from core.fetcher import open_meteo, qweather

__all__ = ["open_meteo", "qweather"]
