"""Shared HTTP POST helper with retry/backoff, for any TypeSafe-System-One
compatible endpoint (the real Jev API, a self-hosted Kev server, etc).

TypeSafe asks clients to back off and retry on 429 (rate limited) and 529
(temporarily overloaded) rather than retrying immediately.
"""

import time

import httpx

RETRYABLE_STATUS_CODES = {429, 529}
MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 1.0


def post_with_retry(url: str, payload: dict, headers: dict, timeout: float = 15) -> httpx.Response:
    """POST to a System-One-compatible endpoint, retrying on 429 / 529."""
    last_exception: Exception | None = None

    for attempt in range(MAX_RETRIES + 1):
        response = httpx.post(url, json=payload, headers=headers, timeout=timeout)

        if response.status_code not in RETRYABLE_STATUS_CODES:
            response.raise_for_status()
            return response

        if attempt < MAX_RETRIES:
            time.sleep(BASE_BACKOFF_SECONDS * (2**attempt))
        else:
            last_exception = httpx.HTTPStatusError(
                f"Endpoint still returning {response.status_code} after {MAX_RETRIES} retries",
                request=response.request,
                response=response,
            )

    raise last_exception
