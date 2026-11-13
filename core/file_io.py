"""Small, reliable filesystem primitives shared by runtime writers."""

from __future__ import annotations

import errno
import os
import threading
import time
import uuid
from pathlib import Path


_ATOMIC_WRITE_LOCK = threading.RLock()
_REPLACE_RETRY_DELAYS = (0.02, 0.05, 0.1, 0.2, 0.4, 0.8)
_MAX_LEGACY_PATH_LENGTH = 240
_MIN_TEMP_TOKEN_LENGTH = 8


def _temporary_path(destination: Path) -> Path:
    """Return a unique sibling path that stays below Windows MAX_PATH."""
    parent_text = os.path.abspath(os.fspath(destination.parent))
    prefix = ".tmp-"
    token = uuid.uuid4().hex
    available = _MAX_LEGACY_PATH_LENGTH - len(parent_text) - 1
    token_length = min(len(token), available - len(prefix))
    if token_length < _MIN_TEMP_TOKEN_LENGTH:
        raise OSError(
            errno.ENAMETOOLONG,
            "destination directory leaves no room for a unique atomic-write "
            "temporary file",
            os.fspath(destination),
        )
    name = prefix + token[:token_length]
    return destination.with_name(name)


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Replace a text file atomically without sharing one temporary path.

    Windows can briefly deny a replace while another request or filesystem
    scanner still has the destination open. Unique temporary names prevent
    writer-to-writer collisions; the lock and bounded retry cover the replace.
    """
    destination = Path(path)
    with _ATOMIC_WRITE_LOCK:
        last_error = None
        for attempt in range(len(_REPLACE_RETRY_DELAYS) + 1):
            temporary = None
            try:
                # Keep directory creation inside the retry window. A report
                # collector can observe or recreate a run directory while an
                # evidence bundle is being finalized.
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = _temporary_path(destination)
                temporary.write_text(content, encoding=encoding)
                try:
                    os.replace(temporary, destination)
                    return
                except (FileNotFoundError, PermissionError):
                    # The parent or destination may be briefly held/replaced
                    # by another process. Let the outer loop recreate a fresh
                    # sibling temporary file before retrying.
                    raise
            except (FileNotFoundError, PermissionError) as exc:
                last_error = exc
                if attempt >= len(_REPLACE_RETRY_DELAYS):
                    raise
                time.sleep(_REPLACE_RETRY_DELAYS[attempt])
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
        if last_error is not None:  # pragma: no cover - loop always returns/raises
            raise last_error
