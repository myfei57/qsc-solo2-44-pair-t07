"""Backends that persist journal entries.

Both backends separate ``append`` from ``flush`` on purpose: a staged entry is
invisible to a reader that opens the backend again, which is how the runtime
models "written but not durable".

A flush does not only make the staged ``append`` lines durable: it also appends
a ``durable`` watermark entry in the same flush.  A commit appends a ``commit``
watermark entry.  On restart the watermark entries are the only thing that
tells the reader how far the previous run actually got, so every byte past the
last watermark is treated as an unresolved tail and never replayed.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

from ..errors import ValidationError
from .codec import (
    ENTRY_COMMIT,
    ENTRY_DURABLE,
    JournalEntry,
    decode_entry,
    encode_entry,
)


WATERMARK_ENTRY_TYPES = (ENTRY_DURABLE, ENTRY_COMMIT)


class LogBackend(Protocol):
    """Storage behind the append only log."""

    def append(self, entry: JournalEntry) -> None:
        """Stage one entry."""

    def flush_now(self) -> None:
        """Make every staged entry durable."""

    def read(self) -> list[JournalEntry]:
        """Return the durable entries in write order."""

    def close(self) -> None:
        """Release any handle held by the backend."""


class MemoryBackend:
    """A backend that keeps staged and durable entries in memory."""

    __slots__ = ("_staged", "_durable")

    def __init__(self) -> None:
        self._staged: list[JournalEntry] = []
        self._durable: list[JournalEntry] = []

    def append(self, entry: JournalEntry) -> None:
        self._staged.append(entry)

    def flush_now(self) -> None:
        if not self._staged:
            return
        self._durable.extend(self._staged)
        self._staged.clear()

    def read(self) -> list[JournalEntry]:
        return list(self._durable)

    def close(self) -> None:
        return None


class FileBackend:
    """A backend that writes one json line per entry.

    On open the journal is replayed byte by byte.  Decoding stops at the first
    line that is not a complete entry -- a kill mid ``write`` leaves exactly
    such a tail -- and the file is truncated at the end of the last durable or
    commit watermark entry.  Every line after that watermark was never
    acknowledged as durable, so it cannot be the position to continue from.
    """

    __slots__ = ("_path", "_handle")

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._recover()
        self._handle = self._path.open("a", encoding="utf-8")

    @property
    def path(self) -> Path:
        return self._path

    def _recover(self) -> None:
        """Drop every line past the last flushed watermark entry."""

        if not self._path.exists():
            return
        data = self._path.read_bytes()
        if not data:
            return
        valid_end = 0
        cursor = 0
        while cursor < len(data):
            newline = data.find(b"\n", cursor)
            if newline == -1:
                break
            line_end = newline + 1
            try:
                entry = decode_entry(data[cursor:newline].decode("utf-8"))
            except (UnicodeDecodeError, ValidationError):
                break
            if entry.entry_type in WATERMARK_ENTRY_TYPES and entry.seq is not None:
                valid_end = line_end
            cursor = line_end
        if cursor != valid_end or valid_end != len(data):
            with self._path.open("ab") as handle:
                handle.truncate(valid_end)
                handle.flush()
                os.fsync(handle.fileno())

    def append(self, entry: JournalEntry) -> None:
        self._handle.write(encode_entry(entry))
        self._handle.write("\n")

    def flush_now(self) -> None:
        self._handle.flush()
        os.fsync(self._handle.fileno())

    def read(self) -> list[JournalEntry]:
        if not self._path.exists():
            return []
        entries: list[JournalEntry] = []
        with self._path.open("r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    entries.append(decode_entry(line))
                except ValidationError as exc:
                    raise ValidationError(
                        "journal file contains an unreadable line", path=str(self._path), line=number
                    ) from exc
        return entries

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()
