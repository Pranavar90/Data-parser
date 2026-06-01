"""
cache.py — Simple in-memory LRU cache for LLM responses.
Avoids re-running the model on identical text (same content = same result at temperature=0).
"""

from collections import OrderedDict
from typing import Any, Optional
import threading
import time


class InMemoryCache:
    def __init__(self, maxsize: int = 500, ttl: Optional[float] = None):
        self._store: OrderedDict = OrderedDict()
        self._maxsize = maxsize
        self._ttl = ttl
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            if key not in self._store:
                return None
            value, ts = self._store[key]
            if self._ttl is not None and (time.time() - ts) > self._ttl:
                del self._store[key]
                return None
            self._store.move_to_end(key)
            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            elif len(self._store) >= self._maxsize:
                self._store.popitem(last=False)
            self._store[key] = (value, time.time())

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._store)


_llm_cache = InMemoryCache(maxsize=500)


def cached_llm_response(key: str) -> Optional[Any]:
    return _llm_cache.get(key)


def store_llm_response(key: str, value: Any) -> None:
    _llm_cache.set(key, value)
