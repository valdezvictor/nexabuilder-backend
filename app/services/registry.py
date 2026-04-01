from typing import Callable, Awaitable, Dict, List

ScraperFn = Callable[[str], Awaitable[List[dict]]]

SCRAPER_REGISTRY: Dict[str, ScraperFn] = {}


def register_scraper(state_code: str, fn: ScraperFn):
    SCRAPER_REGISTRY[state_code.upper()] = fn


def get_scraper(state_code: str) -> ScraperFn:
    fn = SCRAPER_REGISTRY.get(state_code.upper())
    if not fn:
        raise ValueError(f"No scraper registered for state {state_code}")
    return fn
