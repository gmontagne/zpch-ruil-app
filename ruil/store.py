"""Tiny JSON-file storage. One file per collection, atomic writes, one process."""

import json
import os
import tempfile
import threading

_lock = threading.RLock()


class Store:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)

    def _path(self, name):
        return os.path.join(self.data_dir, f"{name}.json")

    def load(self, name, default):
        with _lock:
            try:
                with open(self._path(name), encoding="utf-8") as f:
                    return json.load(f)
            except FileNotFoundError:
                return default

    def save(self, name, data):
        with _lock:
            fd, tmp = tempfile.mkstemp(dir=self.data_dir, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path(name))

    def update(self, name, default, fn):
        """Load, apply fn(data) in place, save. Returns fn's result."""
        with _lock:
            data = self.load(name, default)
            result = fn(data)
            self.save(name, data)
            return result
