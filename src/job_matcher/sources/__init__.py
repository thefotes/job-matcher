"""Public ATS job-board adapters.

Each adapter module exposes ``NAME``, ``careers_url(board)``, ``fetch(http, board,
horizon_days)`` returning a Listing, ``probe(http, board)`` for cheap existence checks and,
when the list omits descriptions, ``detail(http, board, posting)``.
"""

from job_matcher.sources import ashby, greenhouse, lever, recruitee, smartrecruiters, workday

ADAPTERS = {
    module.NAME: module
    for module in (greenhouse, ashby, lever, smartrecruiters, recruitee, workday)
}
PROVIDERS = tuple(ADAPTERS)


def adapter(provider):
    try:
        return ADAPTERS[provider]
    except KeyError:
        raise ValueError(
            f"Unsupported provider {provider!r}; supported: {', '.join(PROVIDERS)}"
        ) from None


def low_trust(provider):
    """A 200 from a guessed token proves little on these providers (vanity/demo accounts)."""
    return getattr(adapter(provider), "LOW_TRUST", False)
