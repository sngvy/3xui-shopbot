"""Чтение числовых параметров из переменных окружения (.env) с безопасными значениями по умолчанию."""

import logging
import os

logger = logging.getLogger(__name__)


def _clamp(value, minimum, maximum):
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def env_int(name: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    """Целое из env. Пусто/не число -> default (с предупреждением), результат зажимается в [minimum, maximum]."""
    raw = (os.getenv(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        logger.warning(f"{name}={raw!r} не целое число — использую {default}")
        value = default
    return _clamp(value, minimum, maximum)


def env_float(
    name: str, default: float, minimum: float | None = None, maximum: float | None = None
) -> float:
    """Дробное число из env. Пусто/не число -> default (с предупреждением), результат зажимается в [minimum, maximum]."""
    raw = (os.getenv(name) or "").strip().replace(",", ".")
    try:
        value = float(raw) if raw else default
    except ValueError:
        logger.warning(f"{name}={raw!r} не число — использую {default}")
        value = default
    return _clamp(value, minimum, maximum)


def env_int_list(name: str, default: list[int], minimum: int | None = None) -> list[int]:
    """Список целых через запятую (например, 72,48,24,1). Пусто/ошибка -> default."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return list(default)
    try:
        values = [int(p) for p in raw.replace(";", ",").split(",") if p.strip()]
        if minimum is not None:
            values = [v for v in values if v >= minimum]
        if not values:
            raise ValueError("пустой список")
        return values
    except ValueError:
        logger.warning(f"{name}={raw!r} не список целых через запятую — использую {default}")
        return list(default)
