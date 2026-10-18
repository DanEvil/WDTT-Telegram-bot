"""Utility functions for retry logic."""

from tenacity import retry, stop_after_attempt, wait_exponential, AsyncRetrying

def retry_async(attempts: int = 3, delay: float = 1.0, max_delay: float = 10.0):
    """
    Simple decorator for async functions to retry on exception.

    Usage:
        @retry_async(attempts=3, delay=1, max_delay=10)
        async def foo(...):
            ...
    """
    def decorator(func):
        @retry(
            reraise=True,
            stop=stop_after_attempt(attempts),
            wait=wait_exponential(multiplier=delay, min=delay, max=max_delay),
        )
        async def wrapper(*args, **kwargs):
            return await func(*args, **kwargs)
        return wrapper
    return wrapper