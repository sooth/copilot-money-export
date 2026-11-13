# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
# cython: cdivision=True
"""
Cython-optimized utilities for fast LevelDB data extraction.

These functions provide C-speed implementations of the hot-path
operations in the financial data extractor.
"""

from cpython.bytes cimport PyBytes_AS_STRING, PyBytes_GET_SIZE
from cpython.bytearray cimport PyByteArray_AS_STRING, PyByteArray_GET_SIZE
from libc.string cimport memcpy, memchr, memcmp
from libc.math cimport fabs


# =============================================================================
# Fast Key Parsing
# =============================================================================

def parse_key_fast(bytes key):
    """
    Parse a LevelDB key into ASCII string components.

    C-optimized version using direct byte access.
    Extracts readable ASCII strings from binary key data.

    Args:
        key: Raw bytes of the LevelDB key

    Returns:
        List of ASCII string components found in the key
    """
    cdef:
        const unsigned char* data = <const unsigned char*>PyBytes_AS_STRING(key)
        Py_ssize_t length = PyBytes_GET_SIZE(key)
        Py_ssize_t i, start, end
        unsigned char byte
        list parts = []
        bint in_string = False

    start = 0
    for i in range(length):
        byte = data[i]
        if 32 <= byte <= 126:  # Printable ASCII range
            if not in_string:
                start = i
                in_string = True
        else:
            if in_string:
                # End of string segment
                if i - start > 2:  # Only keep strings longer than 2 chars
                    parts.append(key[start:i].decode('ascii'))
                in_string = False

    # Don't forget trailing content
    if in_string and length - start > 2:
        parts.append(key[start:length].decode('ascii'))

    return parts


# =============================================================================
# Fast Double Extraction
# =============================================================================

def extract_doubles_fast_c(bytes value, double min_val=0.01, double max_val=1000000.0):
    """
    Extract IEEE 754 doubles from binary data using direct memory access.

    C-optimized version that uses memcpy for unaligned reads.
    Only checks little-endian (Firestore standard).

    Args:
        value: Raw bytes to scan
        min_val: Minimum absolute value to consider valid
        max_val: Maximum absolute value to consider valid

    Returns:
        List of valid double values found
    """
    cdef:
        const unsigned char* data = <const unsigned char*>PyBytes_AS_STRING(value)
        Py_ssize_t length = PyBytes_GET_SIZE(value)
        Py_ssize_t i
        double d
        double abs_d
        list results = []

    if length < 8:
        return results

    # Scan at every byte position for accuracy
    for i in range(length - 7):
        # Direct memory copy as little-endian double
        memcpy(&d, &data[i], 8)

        # Fast validation using fabs from libc
        abs_d = fabs(d)
        if abs_d > min_val and abs_d < max_val:
            # Additional check: not NaN or Inf
            if d == d:  # NaN check: NaN != NaN
                results.append(d)

    return results


# =============================================================================
# Fast Byte Search
# =============================================================================

cdef inline bint _contains_bytes(const char* haystack, Py_ssize_t haystack_len,
                                  const char* needle, Py_ssize_t needle_len) noexcept:
    """
    Check if needle exists within haystack using C-level search.

    Uses a simple but fast search algorithm suitable for short needles.
    """
    cdef:
        Py_ssize_t i, j
        const char* p
        bint match

    if needle_len == 0:
        return True
    if needle_len > haystack_len:
        return False

    # Use memchr to find first byte, then compare rest
    i = 0
    while i <= haystack_len - needle_len:
        # Find first byte of needle
        p = <const char*>memchr(&haystack[i], needle[0], haystack_len - i - needle_len + 1)
        if p == NULL:
            return False

        # Check if full needle matches
        i = p - haystack
        if i + needle_len <= haystack_len:
            # Manual comparison for short strings (faster than memcmp for small sizes)
            if needle_len <= 16:
                match = True
                for j in range(needle_len):
                    if haystack[i + j] != needle[j]:
                        match = False
                        break
                if match:
                    return True
            else:
                # For longer needles, use memcmp
                if memcmp(&haystack[i], needle, needle_len) == 0:
                    return True
        i += 1

    return False


def contains_marker(bytes key, bytes marker):
    """
    Check if key contains the marker bytes using C-level search.

    Much faster than Python's `marker in key` for the hot loop.

    Args:
        key: The bytes to search in
        marker: The bytes to search for

    Returns:
        True if marker is found in key
    """
    cdef:
        const char* key_data = PyBytes_AS_STRING(key)
        Py_ssize_t key_len = PyBytes_GET_SIZE(key)
        const char* marker_data = PyBytes_AS_STRING(marker)
        Py_ssize_t marker_len = PyBytes_GET_SIZE(marker)

    return _contains_bytes(key_data, key_len, marker_data, marker_len)


# =============================================================================
# Batch Filtering
# =============================================================================

def filter_account_entries(db_iterator, bytes marker):
    """
    Filter LevelDB entries, returning only those containing the marker.

    This keeps the hot loop in C, only returning matching entries to Python.

    Args:
        db_iterator: LevelDB iterator (db.RangeIter())
        marker: Bytes to search for in keys (e.g., b'accounts')

    Returns:
        List of (key, value) tuples for matching entries
    """
    cdef:
        list results = []
        bytes key, value
        const char* marker_data = PyBytes_AS_STRING(marker)
        Py_ssize_t marker_len = PyBytes_GET_SIZE(marker)
        const char* key_data
        Py_ssize_t key_len

    for key, value in db_iterator:
        key_data = PyBytes_AS_STRING(key)
        key_len = PyBytes_GET_SIZE(key)

        if _contains_bytes(key_data, key_len, marker_data, marker_len):
            results.append((key, value))

    return results


def filter_and_count(db_iterator, bytes marker):
    """
    Filter entries and return both matches and total count.

    Useful for progress reporting while keeping the loop in C.

    Args:
        db_iterator: LevelDB iterator
        marker: Bytes to search for

    Returns:
        Tuple of (matched_entries_list, total_count)
    """
    cdef:
        list results = []
        bytes key_bytes, value_bytes
        const char* marker_data = PyBytes_AS_STRING(marker)
        Py_ssize_t marker_len = PyBytes_GET_SIZE(marker)
        const char* key_data
        Py_ssize_t key_len
        Py_ssize_t total = 0

    for key, value in db_iterator:
        total += 1
        # Convert bytearray to bytes if needed (leveldb returns bytearray)
        if isinstance(key, bytearray):
            key_bytes = bytes(key)
        else:
            key_bytes = key
        if isinstance(value, bytearray):
            value_bytes = bytes(value)
        else:
            value_bytes = value

        key_data = PyBytes_AS_STRING(key_bytes)
        key_len = PyBytes_GET_SIZE(key_bytes)

        if _contains_bytes(key_data, key_len, marker_data, marker_len):
            results.append((key_bytes, value_bytes))

    return results, total


def filter_keys(key_iterator, bytes marker):
    """
    Return keys containing marker from a keys-only iterator
    (db.RangeIter(include_value=False)). Skipping values avoids materializing
    every value in the DB; fetch matches with db.Get().
    """
    cdef:
        list results = []
        const char* marker_data = PyBytes_AS_STRING(marker)
        Py_ssize_t marker_len = PyBytes_GET_SIZE(marker)

    for key in key_iterator:
        if isinstance(key, bytearray):
            if _contains_bytes(PyByteArray_AS_STRING(key), PyByteArray_GET_SIZE(key),
                               marker_data, marker_len):
                results.append(bytes(key))
        elif _contains_bytes(PyBytes_AS_STRING(key), PyBytes_GET_SIZE(key),
                             marker_data, marker_len):
            results.append(key)

    return results
