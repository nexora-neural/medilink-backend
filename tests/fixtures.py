from unittest.mock import AsyncMock


def synthetic_database_check(*, fail: bool = False) -> AsyncMock:
    # Use a synthetic async check; this fixture never connects to a real database.
    if fail:
        return AsyncMock(side_effect=RuntimeError("Synthetic database failure"))
    return AsyncMock(return_value=None)
