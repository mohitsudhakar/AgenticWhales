"""As-of-date guard — prevents look-ahead bias in backtests.

A backtest replays history, but every data tool we have (yfinance, Alpha Vantage,
news APIs) will happily serve up data past the as-of-date if you don't constrain
the call site. The classic look-ahead bug is "training" on what only became
visible after the decision was made.

The guard is a ContextVar holding the current as-of date. Decorated data accessors
check it on entry and raise `LookAheadViolation` if their `end_date` / `curr_date`
argument is past as-of. In normal (non-backtest) execution the ContextVar is None
and the guard is a no-op — production code pays nothing.

Usage:

    with as_of_date("2024-06-01"):
        # Any decorated dataflow will refuse to return data > 2024-06-01.
        df = get_YFin_data_online("AAPL", "2024-01-01", "2024-06-30")  # raises

Decorating tools:

    @bounded_to_as_of(date_arg="end_date")
    def get_YFin_data_online(symbol, start_date, end_date): ...

If the as-of-date is set but the call would otherwise have requested a future
date, we *truncate* the request to as-of rather than raise — backtest replay
loops typically pass a fixed window and the truncation keeps them simple. The
raise behavior is reserved for the case where the truncated window would be
empty (start_date > as_of), which is unambiguously a bug.
"""

from __future__ import annotations

import datetime as _dt
import functools
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Optional, TypeVar

log = logging.getLogger(__name__)

_AS_OF: ContextVar[Optional[_dt.date]] = ContextVar("aw_as_of_date", default=None)

F = TypeVar("F", bound=Callable)


class LookAheadViolation(RuntimeError):
    """Raised when a backtest data accessor would return data past the as-of date.

    Subclass of RuntimeError (not ValueError) so callers can distinguish a
    look-ahead bug from a malformed-input bug."""


def _parse_date(value) -> Optional[_dt.date]:
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    s = str(value).strip()
    if not s:
        return None
    # Accept YYYY-MM-DD and YYYYMMDD; tolerate trailing time/timezone.
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return _dt.datetime.strptime(s[: len(fmt) + 2 if "T" in s else len(s)], fmt).date()
        except ValueError:
            continue
    try:
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


@contextmanager
def as_of_date(date):
    """Bind an as-of date for the duration of the `with` block.

    Accepts a `date`, `datetime`, or ISO-format string. Nested blocks restore
    the prior value on exit. None unsets (useful in nested test fixtures).
    """
    parsed = _parse_date(date) if date is not None else None
    token = _AS_OF.set(parsed)
    try:
        yield parsed
    finally:
        _AS_OF.reset(token)


def current_as_of() -> Optional[_dt.date]:
    """Return the current as-of date (or None if not inside a `with as_of_date`)."""
    return _AS_OF.get()


def bounded_to_as_of(*, date_arg: str = "end_date", date_arg_pos: Optional[int] = None) -> Callable[[F], F]:
    """Decorator that truncates a data accessor's end-date argument to the current
    as-of date.

    Two ways to identify the argument:
      * `date_arg`: keyword name (default "end_date")
      * `date_arg_pos`: positional index (for legacy untyped tools)

    Behavior:
      * No as-of bound → call passes through unchanged
      * Requested end ≤ as-of → unchanged
      * Requested end > as-of → silently truncated to as-of; debug-logged
      * Truncated window would be empty (start > as-of) → raises `LookAheadViolation`
    """

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            bound = current_as_of()
            if bound is None:
                return fn(*args, **kwargs)
            # Locate the end-date argument.
            end_raw = kwargs.get(date_arg)
            if end_raw is None and date_arg_pos is not None and len(args) > date_arg_pos:
                end_raw = args[date_arg_pos]
            end = _parse_date(end_raw)
            if end is None or end <= bound:
                return fn(*args, **kwargs)
            # Find start to verify the truncated window is non-empty.
            for sk in ("start_date", "from_date", "curr_date"):
                if sk in kwargs:
                    start = _parse_date(kwargs.get(sk))
                    if start and start > bound:
                        raise LookAheadViolation(
                            f"{fn.__name__}: start_date {start} > as_of {bound}; "
                            f"truncated window would be empty"
                        )
                    break
            log.debug("as_of_truncate fn=%s requested=%s truncated_to=%s",
                      fn.__name__, end, bound)
            if date_arg in kwargs:
                kwargs[date_arg] = bound.isoformat()
            elif date_arg_pos is not None:
                args = list(args)
                args[date_arg_pos] = bound.isoformat()
                args = tuple(args)
            return fn(*args, **kwargs)

        wrapper.__aw_bounded__ = True  # introspection for tests
        return wrapper  # type: ignore[return-value]

    return decorator


def assert_as_of(date) -> None:
    """Convenience: raise `LookAheadViolation` if `date` is past current as-of.

    Used inside data tools that can't easily be decorated (e.g. ones that
    construct their own date strings internally)."""
    bound = current_as_of()
    if bound is None:
        return
    when = _parse_date(date)
    if when and when > bound:
        raise LookAheadViolation(f"date {when} > as_of {bound}")
