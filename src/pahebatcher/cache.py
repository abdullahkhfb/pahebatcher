"""Async-safe in-memory TTL cache that evicts its oldest entry."""

import asyncio
import time
from typing import Any


class TTLCache:
    """An async-safe in-memory cache whose entries expire after a TTL.

    When the cache is full, the entry stored longest ago is evicted to make
    room. Reading an entry does not refresh its age.
    """

    def __init__(self, ttl: float = 120.0, max_size: int = 512) -> None:
        """Initializes an empty cache.

        Args:
            ttl: Seconds an entry stays valid after it is stored.
            max_size: Maximum number of entries to keep.
        """
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = asyncio.Lock()
        self._ttl = ttl
        self._max = max_size

    async def get(self, key: str) -> Any:
        """Looks up a cached value.

        Args:
            key: The cache key.

        Returns:
            The cached value, or None if the key is missing or has expired.
            Expired entries are removed.
        """
        async with self._lock:
            if entry := self._data.get(key):
                if time.monotonic() - entry[0] < self._ttl:
                    return entry[1]
                del self._data[key]
        return None

    async def set(self, key: str, val: Any) -> None:
        """Stores a value, evicting the oldest entry if the cache is full.

        Args:
            key: The cache key.
            val: The value to cache.
        """
        async with self._lock:
            if len(self._data) >= self._max:
                oldest = min(self._data, key=lambda k: self._data[k][0])
                del self._data[oldest]
            self._data[key] = (time.monotonic(), val)

    async def clear(self) -> None:
        """Removes every entry."""
        async with self._lock:
            self._data.clear()

    async def discard(self, key: str) -> None:
        """Removes one entry if present.

        Args:
            key: The cache key.
        """
        async with self._lock:
            self._data.pop(key, None)

    def __len__(self) -> int:
        return len(self._data)
