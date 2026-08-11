"""Thread'ler arası paylaşılan dashboard durumu (funding + range)."""

import copy
import threading
import time

_lock = threading.Lock()
_data: dict = {
    "meta": {"started": time.time()},
    "funding": {},
    "ranges": {},
    "sim": {},
}


def update(section: str, payload) -> None:
    with _lock:
        _data[section] = payload


def snapshot() -> dict:
    with _lock:
        return copy.deepcopy(_data)
