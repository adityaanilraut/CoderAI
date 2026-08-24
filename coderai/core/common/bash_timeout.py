""""""

DEFAULT_BASH_TIMEOUT_MS = 10 * 60 * 1000
MIN_BASH_TIMEOUT_MS = 60 * 1000
BASH_TIMEOUT_INCREMENT_MS = 5 * 60 * 1000
BASH_TIMEOUT_DECREMENT_MS = 60 * 1000


def clamp_bash_timeout_ms(
    timeout_ms: float, min_timeout_ms: float | None = MIN_BASH_TIMEOUT_MS
) -> int:
    if not isinstance(timeout_ms, (int, float)) or timeout_ms != timeout_ms:  # NaN check
        return DEFAULT_BASH_TIMEOUT_MS
    minimum = (
        max(1, round(min_timeout_ms))
        if isinstance(min_timeout_ms, (int, float))
        else MIN_BASH_TIMEOUT_MS
    )
    return max(minimum, round(timeout_ms))
