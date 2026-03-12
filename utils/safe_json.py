"""
Atomic JSON write utility to prevent data corruption on crash.

Every JSON-backed store in the bot (proposals, wallets, history, hats roles,
intros) uses this helper instead of a bare ``json.dump`` so that a crash or
power loss mid-write never leaves a half-written file on disk.
"""

import json
import os
import tempfile


def atomic_save(filepath: str, data, indent: int = 2):
    """Persist *data* as pretty-printed JSON at *filepath* atomically.

    Strategy: write to a temporary file in the same directory, then call
    ``os.replace()`` which is guaranteed to be atomic on POSIX and Windows.
    If anything goes wrong during the write, the temp file is cleaned up and
    the original file is left untouched.

    Args:
        filepath: Destination path for the JSON file.
        data: Any JSON-serialisable Python object.
        indent: Indentation level for readability (default 2 spaces).
    """
    dirpath = os.path.dirname(filepath)
    # Ensure the parent directory exists (safe for first-run scenarios).
    os.makedirs(dirpath, exist_ok=True)

    # Create a temp file in the *same* directory so os.replace is a same-device
    # rename, which is what makes the operation atomic.
    fd, tmp_path = tempfile.mkstemp(dir=dirpath, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=indent)
        # Atomic swap: either the old file or the new file exists, never neither.
        os.replace(tmp_path, filepath)
    except BaseException:
        # Clean up temp file on any failure so we don't leak orphan files.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
