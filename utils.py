#!/usr/bin/env python3
"""
Shared utilities for Copilot Money data extraction.
Contains common functions, constants, and the SafeDatabaseReader context manager.
"""

import os
import sqlite3
import struct
import shutil
import subprocess
import tempfile
from typing import Optional

import leveldb
import numpy as np


# =============================================================================
# Constants
# =============================================================================

# Balance range for valid financial values
BALANCE_MIN = 0.01
BALANCE_MAX = 1_000_000

# Tolerance levels for value matching
TOLERANCE_EXACT = 0.01      # For exact value matches (cents)
TOLERANCE_CENTS = 0.50      # For cent-level tolerance
TOLERANCE_DOLLARS = 50.0    # For dollar-level tolerance (investments)
TOLERANCE_CLOSE = 500.0     # For approximate matches

# LevelDB configuration
WRITE_BUFFER_SIZE = 4       # Minimal write buffer
MAX_OPEN_FILES = 50         # Limit file handles


# =============================================================================
# Shared Functions
# =============================================================================

def parse_key(key: bytes) -> list[str]:
    """
    Parse a LevelDB key into ASCII string components.

    Extracts readable ASCII strings from binary key data, filtering out
    non-printable characters and short fragments.

    Args:
        key: Raw bytes of the LevelDB key

    Returns:
        List of ASCII string components found in the key
    """
    parts: list[str] = []
    current: list[str] = []

    for byte in key:
        if 32 <= byte <= 126:  # Printable ASCII range
            current.append(chr(byte))
        else:
            if len(current) > 2:  # Only keep strings longer than 2 chars
                parts.append(''.join(current))
            current = []

    # Don't forget trailing content
    if len(current) > 2:
        parts.append(''.join(current))

    return parts


def extract_doubles(
    value: bytes,
    min_val: float = BALANCE_MIN,
    max_val: float = BALANCE_MAX
) -> list[tuple[int, float]]:
    """
    Extract IEEE 754 double values from binary data.

    Scans through the binary data looking for 8-byte sequences that decode
    to valid double values within the specified range.

    Args:
        value: Raw bytes to scan
        min_val: Minimum absolute value to consider valid
        max_val: Maximum absolute value to consider valid

    Returns:
        List of tuples containing (byte_offset, decoded_value)
    """
    doubles: list[tuple[int, float]] = []

    if len(value) < 8:
        return doubles

    for i in range(len(value) - 7):
        try:
            # Try little-endian (most common in Firestore)
            d = struct.unpack('<d', value[i:i+8])[0]
            if min_val < abs(d) < max_val:
                doubles.append((i, d))
        except struct.error:
            pass

        try:
            # Try big-endian as fallback
            d = struct.unpack('>d', value[i:i+8])[0]
            if min_val < abs(d) < max_val:
                doubles.append((i, d))
        except struct.error:
            pass

    return doubles


def extract_doubles_fast(
    value: bytes,
    min_val: float = BALANCE_MIN,
    max_val: float = BALANCE_MAX
) -> list[float]:
    """
    Fast extraction of IEEE 754 doubles using numpy vectorization.

    This is optimized for speed - uses only little-endian (Firestore standard)
    and processes all 8-byte aligned positions at once using numpy.

    Args:
        value: Raw bytes to scan
        min_val: Minimum absolute value to consider valid
        max_val: Maximum absolute value to consider valid

    Returns:
        List of valid double values found (no offsets, just values)
    """
    if len(value) < 8:
        return []

    # Ensure we have enough bytes for at least one double
    # Truncate to 8-byte alignment
    usable_len = (len(value) // 8) * 8
    if usable_len < 8:
        return []

    try:
        # Interpret bytes as array of little-endian doubles
        arr = np.frombuffer(value[:usable_len], dtype='<f8')

        # Filter to valid financial values (vectorized)
        abs_arr = np.abs(arr)
        mask = (abs_arr > min_val) & (abs_arr < max_val) & np.isfinite(arr)

        return arr[mask].tolist()
    except (ValueError, TypeError):
        # Fallback to empty list on any numpy errors
        return []


def is_copilot_running() -> bool:
    """
    Check if the Copilot Money app is currently running.

    Used to determine if it's safe to access the database directly
    without making a copy first.

    Returns:
        True if Copilot app is running, False otherwise
    """
    try:
        result = subprocess.run(
            ['pgrep', '-x', 'Copilot'],
            capture_output=True,
            timeout=5
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        # If we can't check, assume it might be running (safer)
        return True


def extract_floats(
    value: bytes,
    min_val: float = BALANCE_MIN,
    max_val: float = BALANCE_MAX
) -> list[tuple[int, float]]:
    """
    Extract IEEE 754 float (32-bit) values from binary data.

    Args:
        value: Raw bytes to scan
        min_val: Minimum absolute value to consider valid
        max_val: Maximum absolute value to consider valid

    Returns:
        List of tuples containing (byte_offset, decoded_value)
    """
    floats: list[tuple[int, float]] = []

    if len(value) < 4:
        return floats

    for i in range(len(value) - 3):
        try:
            f = struct.unpack('<f', value[i:i+4])[0]
            if min_val < abs(f) < max_val:
                floats.append((i, f))
        except struct.error:
            pass

    return floats


def create_safe_db_copy(source_path: str) -> tuple[str, str]:
    """
    Create a temporary copy of a LevelDB database for safe reading.

    Creates a complete copy of the database directory, removes the LOCK file
    from the copy to allow opening, and returns paths to both the temp
    directory (for cleanup) and the database copy.

    Args:
        source_path: Path to the original LevelDB database directory

    Returns:
        Tuple of (temp_directory_path, database_copy_path)

    Raises:
        FileNotFoundError: If source_path does not exist
    """
    if not os.path.exists(source_path):
        raise FileNotFoundError(f"Database not found at {source_path}")

    temp_dir = tempfile.mkdtemp(prefix="copilot_safe_")
    temp_db_path = os.path.join(temp_dir, "db_copy")

    # Copy entire database directory
    shutil.copytree(source_path, temp_db_path)

    # Remove LOCK file from copy if it exists
    lock_file = os.path.join(temp_db_path, "LOCK")
    if os.path.exists(lock_file):
        os.remove(lock_file)

    return temp_dir, temp_db_path


# Live Copilot data lives under these container paths. Opening the Firestore
# LevelDB here with our (foreign) leveldb build is read-WRITE: it replays the
# WAL, rewrites MANIFEST, and deletes log files on recovery, producing on-disk
# state Copilot's own reader rejects -> the app won't launch. SQLite in the
# group container is less fragile but we still never open the live file.
# So we NEVER open anything under these markers unless it is a throwaway copy.
_LIVE_MARKERS = (
    "com.copilot.production",
    "group.com.copilot.production",
)


def assert_safe_to_open(path: str) -> None:
    """Raise unless `path` is a throwaway copy (never the live Copilot DB).

    The single chokepoint that makes live-DB corruption impossible: any code
    about to call leveldb.LevelDB(), plyvel.DB(), or sqlite3.connect() on a
    Copilot database should pass its path here first.
    """
    real = os.path.realpath(path)
    tmp = os.path.realpath(tempfile.gettempdir())
    if any(marker in real for marker in _LIVE_MARKERS) and not real.startswith(tmp):
        raise RuntimeError(
            f"REFUSING to open the live Copilot database at:\n  {real}\n"
            "Opening it can corrupt it (the app stops launching). "
            "Copy it first (create_safe_db_copy / open_sqlite_copy) and open the copy."
        )


def open_db_copy(live_path: str):
    """Copy the live DB to temp and open the COPY read-only-safely.

    Returns (db, temp_dir). Caller must cleanup_temp_dir(temp_dir) when done.
    The live database is only ever read at the filesystem level (copytree);
    leveldb is only ever pointed at the throwaway copy.
    """
    import leveldb  # local import keeps utils import-light
    temp_dir, copy_path = create_safe_db_copy(live_path)
    assert_safe_to_open(copy_path)
    db = leveldb.LevelDB(copy_path, create_if_missing=False)
    return db, temp_dir


def open_sqlite_copy(live_path: str) -> tuple[sqlite3.Connection, str]:
    """Copy a live SQLite DB (plus WAL/SHM) to temp and open the COPY read-only.

    Returns (connection, temp_dir). Caller must close the connection and
    cleanup_temp_dir(temp_dir) when done. The live files are only read at the
    filesystem level (copy2); sqlite3 is only pointed at the throwaway copy.
    """
    if not os.path.exists(live_path):
        raise FileNotFoundError(f"Database not found at {live_path}")

    temp_dir = tempfile.mkdtemp(prefix="copilot_sqlite_")
    basename = os.path.basename(live_path)
    for suffix in ("", "-wal", "-shm"):
        src = live_path + suffix
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(temp_dir, basename + suffix))

    copy_path = os.path.join(temp_dir, basename)
    assert_safe_to_open(copy_path)
    conn = sqlite3.connect(f"file:{copy_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn, temp_dir


def default_export_dir() -> str:
    """Canonical directory for transaction CSV and balance JSON exports.

    Override with COPILOT2_EXPORT_DIR. Default is Application Support so
    exports are not dumped into the git working tree.
    """
    override = os.environ.get("COPILOT2_EXPORT_DIR")
    if override:
        return os.path.expanduser(override)
    return os.path.join(
        os.path.expanduser("~"),
        "Library",
        "Application Support",
        "copilot2",
    )


def cleanup_temp_dir(temp_dir: str) -> None:
    """
    Safely clean up a temporary directory.

    Args:
        temp_dir: Path to temporary directory to remove
    """
    if temp_dir and os.path.exists(temp_dir):
        try:
            shutil.rmtree(temp_dir)
        except OSError:
            pass  # Best effort cleanup


# =============================================================================
# Context Manager
# =============================================================================

class SafeDatabaseReader:
    """
    Context manager for safely reading a LevelDB database.

    Creates a temporary copy of the database to prevent corruption of the
    original, and ensures proper cleanup of both the database connection
    and temporary files.

    Usage:
        with SafeDatabaseReader(db_path) as db:
            for key, value in db.RangeIter():
                process(key, value)

    Args:
        source_path: Path to the original LevelDB database
        use_copy: If True (default), work on a copy. If False, open original.
    """

    def __init__(self, source_path: str, use_copy: bool = True):
        self.source_path = source_path
        self.use_copy = use_copy
        self.temp_dir: Optional[str] = None
        self.db_path: str = source_path
        self.db: Optional[leveldb.LevelDB] = None

    def __enter__(self) -> leveldb.LevelDB:
        """Open the database (or a copy) and return it."""
        if self.use_copy:
            self.temp_dir, self.db_path = create_safe_db_copy(self.source_path)

        self.db = leveldb.LevelDB(
            self.db_path,
            create_if_missing=False,
            error_if_exists=False,
            write_buffer_size=WRITE_BUFFER_SIZE,
            max_open_files=MAX_OPEN_FILES,
            paranoid_checks=False
        )

        return self.db

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Close database and clean up temporary files."""
        # leveldb.LevelDB doesn't have a close method - it's handled by garbage collection
        self.db = None

        if self.temp_dir:
            cleanup_temp_dir(self.temp_dir)

        # Don't suppress exceptions
        return None


def is_value_match(
    found: float,
    target: float,
    tolerance: float = TOLERANCE_EXACT
) -> bool:
    """
    Check if a found value matches a target within tolerance.

    Args:
        found: The value found in the database
        target: The expected target value
        tolerance: Maximum allowed difference

    Returns:
        True if values match within tolerance
    """
    return abs(found - target) < tolerance


def calculate_similarity(found: float, target: float) -> float:
    """
    Calculate percentage similarity between two values.

    Args:
        found: The value found in the database
        target: The expected target value

    Returns:
        Similarity percentage (0-100, can exceed 100 if found > target)
    """
    if target == 0:
        return 0.0
    return 100 - (abs(found - target) / target * 100)
