from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


def shadow_run(
    primary_fn: Callable[..., Any],
    shadow_fn: Callable[..., Any],
    *args: Any,
    label: str = "",
    **kwargs: Any,
) -> Any:
    """Call primary_fn and shadow_fn with the same args.

    Returns the primary result. If shadow_fn raises an exception it is caught
    and logged without propagating. If the shadow result differs from the
    primary result a divergence warning is logged.
    """
    primary_result = primary_fn(*args, **kwargs)

    try:
        shadow_result = shadow_fn(*args, **kwargs)
        if shadow_result != primary_result:
            logger.warning(
                "shadow_run divergence label=%r primary=%r shadow=%r",
                label,
                primary_result,
                shadow_result,
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "shadow_run exception in shadow function label=%r: %s",
            label,
            exc,
        )

    return primary_result
