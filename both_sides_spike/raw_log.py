from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import struct
import time
import uuid
import zlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Iterator

try:
    import zstandard as zstd
except ImportError:  # pragma: no cover - exercised by deployment prerequisite checks
    zstd = None

from . import SCHEMA_VERSION


ARCHIVE_MAGIC = b"BSSRAW2\0"
SEGMENT_MAGIC = b"SEG2"
SEGMENT_PREFIX = struct.Struct(">II")
WS_RECORD = struct.Struct(">BIQQQI")
REST_RECORD = struct.Struct(">BIQQHII")
CONTROL_RECORD = struct.Struct(">BIQQI")
RECORD_WS = 1
RECORD_REST = 2
RECORD_CONTROL = 3
ZERO_HASH = bytes(32)


class RawLogFormatError(ValueError):
    pass


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unavailable"


def _canonical(value: Any, *, newline: bool = True) -> bytes:
    suffix = "\n" if newline else ""
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + suffix).encode()


def _read_exact(handle: BinaryIO, size: int, label: str) -> bytes:
    value = handle.read(size)
    if len(value) != size:
        raise RawLogFormatError(f"truncated {label}: expected {size} bytes, found {len(value)}")
    return value


def _open_archive(path: Path) -> BinaryIO:
    raw = path.open("rb")
    prefix = raw.read(2)
    raw.close()
    if prefix == b"\x1f\x8b":
        return gzip.open(path, "rb")
    return path.open("rb")


@dataclass(frozen=True)
class RawLogAudit:
    ok: bool
    frame_count: int
    control_count: int
    errors: tuple[str, ...]


class LatencyHistogram:
    """Bounded-memory receive-to-durable latency histogram."""

    def __init__(self, *, bucket_width_ns: int = 100_000):
        self.bucket_width_ns = bucket_width_ns
        self.count = 0
        self.total_ns = 0
        self.maximum_ns = 0
        self.buckets: Counter[int] = Counter()

    def add(self, latency_ns: int) -> None:
        value = max(0, int(latency_ns))
        self.count += 1
        self.total_ns += value
        self.maximum_ns = max(self.maximum_ns, value)
        self.buckets[value // self.bucket_width_ns] += 1

    def percentile_ns(self, quantile: float) -> int | None:
        if not self.count:
            return None
        target = max(1, int(self.count * quantile + 0.999999999))
        cumulative = 0
        for bucket, count in sorted(self.buckets.items()):
            cumulative += count
            if cumulative >= target:
                return (bucket + 1) * self.bucket_width_ns
        return self.maximum_ns

    def summary(self) -> dict[str, int | float | None]:
        def ms(value: int | None) -> float | None:
            return value / 1_000_000 if value is not None else None

        return {
            "count": self.count,
            "bucket_width_ns": self.bucket_width_ns,
            "p50_ms": ms(self.percentile_ns(0.50)),
            "p95_ms": ms(self.percentile_ns(0.95)),
            "p99_ms": ms(self.percentile_ns(0.99)),
            "maximum_ms": ms(self.maximum_ns) if self.count else None,
            "mean_ms": (self.total_ns / self.count / 1_000_000) if self.count else None,
        }


class DurableRawLog:
    """Append-only raw evidence log.

    ``legacy_jsonl`` preserves compatibility with the original smoke archive.
    ``segmented_v2`` stores compact binary records in independently compressed,
    hash-chained segments. A segment is fsynced at most ``durability_window_ms``
    after its first buffered WebSocket frame when the caller invokes
    :meth:`flush_due` while waiting for network input.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        collector_run_id: str | None = None,
        connection_epoch: int = 1,
        storage_format: str = "legacy_jsonl",
        durability_window_ms: int = 100,
        compression: str = "zlib",
        compression_level: int = 6,
    ):
        if storage_format not in {"legacy_jsonl", "segmented_v2"}:
            raise ValueError(f"unsupported storage format: {storage_format}")
        if not 1 <= durability_window_ms <= 1_000:
            raise ValueError("durability_window_ms must be between 1 and 1000")
        if compression not in {"zlib", "zstd"}:
            raise ValueError(f"unsupported compression: {compression}")
        if compression == "zlib" and not 0 <= compression_level <= 9:
            raise ValueError("zlib compression_level must be between 0 and 9")
        if compression == "zstd" and not 1 <= compression_level <= 22:
            raise ValueError("zstd compression_level must be between 1 and 22")
        if compression == "zstd" and zstd is None:
            raise RuntimeError("zstandard package is required for zstd archives")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.collector_run_id = collector_run_id or str(uuid.uuid4())
        self.process_instance_id = str(uuid.uuid4())
        self.host_boot_id = _boot_id()
        self.connection_epoch = connection_epoch
        self.local_frame_index = 0
        self.storage_format = storage_format
        self.durability_window_ms = durability_window_ms
        self.compression = compression
        self.compression_level = compression_level
        self._latency_histogram = LatencyHistogram()
        self._sealed = False
        self._terminal_summary: dict[str, Any] | None = None
        self._handle: BinaryIO
        self._segment_buffer = bytearray()
        self._segment_frame_received_ns: list[int] = []
        self._segment_index = 0
        self._segment_record_count = 0
        self._segment_first_monotonic_ns: int | None = None
        self._segment_last_monotonic_ns: int | None = None
        self._previous_segment_hash = ZERO_HASH

        if storage_format == "legacy_jsonl":
            self._handle = self.path.open("ab", buffering=0)
        else:
            if self.path.exists() and self.path.stat().st_size:
                raise FileExistsError(f"segmented archive already exists: {self.path}")
            self._handle = self.path.open("wb", buffering=0)
            file_header = {
                "archive_schema": "both_sides_segmented_raw_v2",
                "record_schema": SCHEMA_VERSION,
                "collector_run_id": self.collector_run_id,
                "process_instance_id": self.process_instance_id,
                "host_boot_id": self.host_boot_id,
                "compression": self.compression,
                "compression_level": self.compression_level,
                "durability_window_ms": self.durability_window_ms,
            }
            encoded = _canonical(file_header, newline=False)
            self._handle.write(ARCHIVE_MAGIC + struct.pack(">I", len(encoded)) + encoded)
            os.fsync(self._handle.fileno())

    def set_epoch(self, epoch: int) -> None:
        self.connection_epoch = epoch

    @property
    def durable_latency_count(self) -> int:
        return self._latency_histogram.count

    def durable_latency_summary(self) -> dict[str, int | float | None]:
        return self._latency_histogram.summary()

    @property
    def final_chain_hash(self) -> str | None:
        if self.storage_format != "segmented_v2" or self._segment_index == 0:
            return None
        return self._previous_segment_hash.hex()

    @property
    def segment_count(self) -> int:
        return self._segment_index

    @property
    def sealed(self) -> bool:
        return self._sealed

    def _ensure_writable(self) -> None:
        if self._sealed:
            raise RuntimeError("raw archive is sealed")
        if self._handle.closed:
            raise RuntimeError("raw archive is closed")

    def _append_segment_record(
        self,
        record: bytes,
        *,
        received_monotonic_ns: int | None,
        track_frame_latency: bool,
        force_flush: bool,
    ) -> None:
        if (
            self._segment_buffer
            and received_monotonic_ns is not None
            and self._segment_first_monotonic_ns is not None
            and received_monotonic_ns - self._segment_first_monotonic_ns >= self.durability_window_ms * 1_000_000
        ):
            self._flush_segment()
        self._segment_buffer.extend(record)
        self._segment_record_count += 1
        if received_monotonic_ns is not None:
            if self._segment_first_monotonic_ns is None:
                self._segment_first_monotonic_ns = received_monotonic_ns
            self._segment_last_monotonic_ns = received_monotonic_ns
            if track_frame_latency:
                self._segment_frame_received_ns.append(received_monotonic_ns)
        if force_flush:
            self._flush_segment()

    def append_frame(
        self,
        raw: bytes | str,
        *,
        force_flush: bool = True,
        subscription_set_hash: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_writable()
        payload = raw.encode() if isinstance(raw, str) else bytes(raw)
        received_wall = time.time_ns()
        received_mono = time.monotonic_ns()
        self.local_frame_index += 1
        record = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "WS_FRAME",
            "collector_run_id": self.collector_run_id,
            "process_instance_id": self.process_instance_id,
            "host_boot_id": self.host_boot_id,
            "connection_epoch": self.connection_epoch,
            "local_frame_index": self.local_frame_index,
            "received_wall_utc_ns": received_wall,
            "received_monotonic_ns": received_mono,
            "subscription_set_hash": subscription_set_hash,
            "raw_encoding": "base64",
            "raw_payload": base64.b64encode(payload).decode("ascii"),
            "raw_sha256": hashlib.sha256(payload).hexdigest(),
            "monotonic_clock_name": "CLOCK_MONOTONIC",
            "flush_mode": "fsync" if force_flush else "segmented",
        }
        if self.storage_format == "legacy_jsonl":
            self._handle.write(_canonical(record))
            if force_flush:
                os.fsync(self._handle.fileno())
                durable_at = time.monotonic_ns()
                self._latency_histogram.add(durable_at - received_mono)
        else:
            binary = WS_RECORD.pack(
                RECORD_WS,
                self.connection_epoch,
                self.local_frame_index,
                received_wall,
                received_mono,
                len(payload),
            ) + payload
            self._append_segment_record(
                binary,
                received_monotonic_ns=received_mono,
                track_frame_latency=True,
                force_flush=force_flush,
            )
        return record

    def append_rest(
        self,
        raw: bytes,
        *,
        url: str,
        request_started_wall_ns: int,
        response_received_wall_ns: int,
        status_code: int,
        force_flush: bool = True,
    ) -> dict[str, Any]:
        self._ensure_writable()
        payload = bytes(raw)
        record = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "REST_FRAME",
            "collector_run_id": self.collector_run_id,
            "process_instance_id": self.process_instance_id,
            "host_boot_id": self.host_boot_id,
            "connection_epoch": self.connection_epoch,
            "url": url,
            "status_code": status_code,
            "request_started_wall_utc_ns": request_started_wall_ns,
            "response_received_wall_utc_ns": response_received_wall_ns,
            "raw_encoding": "base64",
            "raw_payload": base64.b64encode(payload).decode("ascii"),
            "raw_sha256": hashlib.sha256(payload).hexdigest(),
        }
        if self.storage_format == "legacy_jsonl":
            self._handle.write(_canonical(record))
            if force_flush:
                os.fsync(self._handle.fileno())
        else:
            url_bytes = url.encode()
            binary = REST_RECORD.pack(
                RECORD_REST,
                self.connection_epoch,
                request_started_wall_ns,
                response_received_wall_ns,
                status_code,
                len(url_bytes),
                len(payload),
            ) + url_bytes + payload
            self._append_segment_record(
                binary,
                received_monotonic_ns=time.monotonic_ns(),
                track_frame_latency=False,
                force_flush=force_flush,
            )
        return record

    def append_control(
        self,
        event_type: str,
        details: dict[str, Any] | None = None,
        *,
        force_flush: bool = True,
    ) -> dict[str, Any]:
        self._ensure_writable()
        received_wall = time.time_ns()
        received_mono = time.monotonic_ns()
        record = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "CONTROL",
            "event_type": event_type,
            "collector_run_id": self.collector_run_id,
            "process_instance_id": self.process_instance_id,
            "host_boot_id": self.host_boot_id,
            "connection_epoch": self.connection_epoch,
            "received_wall_utc_ns": received_wall,
            "received_monotonic_ns": received_mono,
            "details": details or {},
        }
        if self.storage_format == "legacy_jsonl":
            self._handle.write(_canonical(record))
            if force_flush:
                os.fsync(self._handle.fileno())
        else:
            body = _canonical({"event_type": event_type, "details": details or {}}, newline=False)
            binary = CONTROL_RECORD.pack(
                RECORD_CONTROL,
                self.connection_epoch,
                received_wall,
                received_mono,
                len(body),
            ) + body
            self._append_segment_record(
                binary,
                received_monotonic_ns=received_mono,
                track_frame_latency=False,
                force_flush=force_flush,
            )
        return record

    def _flush_segment(self) -> None:
        if not self._segment_buffer:
            return
        uncompressed = bytes(self._segment_buffer)
        if self.compression == "zstd":
            assert zstd is not None
            compressed = zstd.ZstdCompressor(
                level=self.compression_level,
                write_checksum=True,
                write_content_size=True,
            ).compress(uncompressed)
        else:
            compressed = zlib.compress(uncompressed, self.compression_level)
        self._segment_index += 1
        header = {
            "segment_index": self._segment_index,
            "record_count": self._segment_record_count,
            "first_received_monotonic_ns": self._segment_first_monotonic_ns,
            "last_received_monotonic_ns": self._segment_last_monotonic_ns,
            "uncompressed_bytes": len(uncompressed),
            "compressed_bytes": len(compressed),
            "uncompressed_sha256": hashlib.sha256(uncompressed).hexdigest(),
            "previous_segment_sha256": self._previous_segment_hash.hex(),
        }
        encoded_header = _canonical(header, newline=False)
        segment_hash = hashlib.sha256(self._previous_segment_hash + encoded_header + compressed).digest()
        self._handle.write(
            SEGMENT_MAGIC
            + SEGMENT_PREFIX.pack(len(encoded_header), len(compressed))
            + encoded_header
            + compressed
            + segment_hash
        )
        os.fsync(self._handle.fileno())
        durable_at = time.monotonic_ns()
        for received_ns in self._segment_frame_received_ns:
            self._latency_histogram.add(durable_at - received_ns)
        self._previous_segment_hash = segment_hash
        self._segment_buffer.clear()
        self._segment_frame_received_ns.clear()
        self._segment_record_count = 0
        self._segment_first_monotonic_ns = None
        self._segment_last_monotonic_ns = None

    def seconds_until_flush(self, default: float = 2.0) -> float:
        if self.storage_format != "segmented_v2" or not self._segment_buffer or self._segment_first_monotonic_ns is None:
            return default
        deadline = self._segment_first_monotonic_ns + self.durability_window_ms * 1_000_000
        return max(0.0, (deadline - time.monotonic_ns()) / 1_000_000_000)

    def flush(self, *, force: bool = False) -> None:
        if self.storage_format == "legacy_jsonl":
            if force:
                os.fsync(self._handle.fileno())
            return
        if force or self.seconds_until_flush() <= 0:
            self._flush_segment()

    def flush_due(self) -> None:
        self.flush(force=False)

    def seal(self, reason: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._terminal_summary is not None:
            return dict(self._terminal_summary)
        terminal_details = {"reason": reason, **(details or {})}
        self.append_control("RUN_TERMINAL", terminal_details, force_flush=True)
        self._sealed = True
        self._terminal_summary = {
            "event_type": "RUN_TERMINAL",
            "reason": reason,
            "sealed_at_wall_utc_ns": time.time_ns(),
            "segment_count": self.segment_count,
            "final_chain_sha256": self.final_chain_hash,
            "durable_latency": self.durable_latency_summary(),
        }
        return dict(self._terminal_summary)

    def close(self) -> None:
        if not self._handle.closed:
            if self.storage_format == "segmented_v2":
                self._flush_segment()
            os.fsync(self._handle.fileno())
            self._handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def raw_payload_bytes(row: dict[str, Any]) -> bytes:
    payload = row.get("raw_payload_bytes")
    if isinstance(payload, bytes):
        return payload
    return base64.b64decode(row["raw_payload"], validate=True)


def _iter_segment_records(uncompressed: bytes, file_header: dict[str, Any], expected_count: int) -> Iterator[dict[str, Any]]:
    view = memoryview(uncompressed)
    offset = 0
    count = 0
    global_fields = {
        "schema_version": file_header.get("record_schema"),
        "collector_run_id": file_header.get("collector_run_id"),
        "process_instance_id": file_header.get("process_instance_id"),
        "host_boot_id": file_header.get("host_boot_id"),
    }
    while offset < len(view):
        record_type = int(view[offset])
        if record_type == RECORD_WS:
            if len(view) - offset < WS_RECORD.size:
                raise RawLogFormatError("truncated WS record header")
            _, epoch, index, wall, mono, payload_size = WS_RECORD.unpack_from(view, offset)
            offset += WS_RECORD.size
            if len(view) - offset < payload_size:
                raise RawLogFormatError("truncated WS record payload")
            payload = bytes(view[offset:offset + payload_size])
            offset += payload_size
            yield {
                **global_fields,
                "record_type": "WS_FRAME",
                "connection_epoch": epoch,
                "local_frame_index": index,
                "received_wall_utc_ns": wall,
                "received_monotonic_ns": mono,
                "raw_payload_bytes": payload,
                "raw_encoding": "binary",
                "monotonic_clock_name": "CLOCK_MONOTONIC",
                "flush_mode": "segmented",
            }
        elif record_type == RECORD_REST:
            if len(view) - offset < REST_RECORD.size:
                raise RawLogFormatError("truncated REST record header")
            _, epoch, request_wall, response_wall, status, url_size, payload_size = REST_RECORD.unpack_from(view, offset)
            offset += REST_RECORD.size
            required = url_size + payload_size
            if len(view) - offset < required:
                raise RawLogFormatError("truncated REST record payload")
            url = bytes(view[offset:offset + url_size]).decode()
            offset += url_size
            payload = bytes(view[offset:offset + payload_size])
            offset += payload_size
            yield {
                **global_fields,
                "record_type": "REST_FRAME",
                "connection_epoch": epoch,
                "url": url,
                "status_code": status,
                "request_started_wall_utc_ns": request_wall,
                "response_received_wall_utc_ns": response_wall,
                "raw_payload_bytes": payload,
                "raw_encoding": "binary",
            }
        elif record_type == RECORD_CONTROL:
            if len(view) - offset < CONTROL_RECORD.size:
                raise RawLogFormatError("truncated CONTROL record header")
            _, epoch, wall, mono, body_size = CONTROL_RECORD.unpack_from(view, offset)
            offset += CONTROL_RECORD.size
            if len(view) - offset < body_size:
                raise RawLogFormatError("truncated CONTROL record body")
            body = json.loads(bytes(view[offset:offset + body_size]))
            offset += body_size
            yield {
                **global_fields,
                "record_type": "CONTROL",
                "event_type": body.get("event_type"),
                "connection_epoch": epoch,
                "received_wall_utc_ns": wall,
                "received_monotonic_ns": mono,
                "details": body.get("details") or {},
            }
        else:
            raise RawLogFormatError(f"unknown compact record type: {record_type}")
        count += 1
    if count != expected_count:
        raise RawLogFormatError(f"segment record count mismatch: expected {expected_count}, decoded {count}")


def _iter_segmented(handle: BinaryIO) -> Iterator[dict[str, Any]]:
    header_size = struct.unpack(">I", _read_exact(handle, 4, "archive header length"))[0]
    file_header = json.loads(_read_exact(handle, header_size, "archive header"))
    previous_hash = ZERO_HASH
    expected_segment = 1
    while True:
        marker = handle.read(len(SEGMENT_MAGIC))
        if not marker:
            break
        if len(marker) != len(SEGMENT_MAGIC):
            raise RawLogFormatError("truncated segment marker")
        if marker != SEGMENT_MAGIC:
            raise RawLogFormatError("invalid segment marker")
        header_len, compressed_len = SEGMENT_PREFIX.unpack(_read_exact(handle, SEGMENT_PREFIX.size, "segment prefix"))
        encoded_header = _read_exact(handle, header_len, "segment header")
        compressed = _read_exact(handle, compressed_len, "compressed segment")
        stored_hash = _read_exact(handle, 32, "segment hash")
        calculated_hash = hashlib.sha256(previous_hash + encoded_header + compressed).digest()
        if stored_hash != calculated_hash:
            raise RawLogFormatError(f"segment {expected_segment} hash mismatch")
        header = json.loads(encoded_header)
        if int(header.get("segment_index", -1)) != expected_segment:
            raise RawLogFormatError(f"segment index mismatch: expected {expected_segment}")
        if header.get("previous_segment_sha256") != previous_hash.hex():
            raise RawLogFormatError(f"segment {expected_segment} previous hash mismatch")
        if int(header.get("compressed_bytes", -1)) != len(compressed):
            raise RawLogFormatError(f"segment {expected_segment} compressed length mismatch")
        compression = str(file_header.get("compression") or "zlib")
        try:
            if compression == "zstd":
                if zstd is None:
                    raise RawLogFormatError("zstandard package is required to read this archive")
                uncompressed = zstd.ZstdDecompressor().decompress(compressed)
            elif compression == "zlib":
                uncompressed = zlib.decompress(compressed)
            else:
                raise RawLogFormatError(f"unsupported archive compression: {compression}")
        except RawLogFormatError:
            raise
        except Exception as exc:
            raise RawLogFormatError(f"segment {expected_segment} decompression failed: {exc}") from exc
        if int(header.get("uncompressed_bytes", -1)) != len(uncompressed):
            raise RawLogFormatError(f"segment {expected_segment} uncompressed length mismatch")
        if header.get("uncompressed_sha256") != hashlib.sha256(uncompressed).hexdigest():
            raise RawLogFormatError(f"segment {expected_segment} uncompressed hash mismatch")
        yield from _iter_segment_records(uncompressed, file_header, int(header.get("record_count", -1)))
        previous_hash = stored_hash
        expected_segment += 1


def iter_raw_records(path: Path | str) -> Iterator[dict[str, Any]]:
    archive = Path(path)
    with _open_archive(archive) as handle:
        prefix = handle.read(len(ARCHIVE_MAGIC))
        if prefix == ARCHIVE_MAGIC:
            yield from _iter_segmented(handle)
            return
        handle.seek(0)
        for line_number, raw_line in enumerate(handle, 1):
            try:
                yield json.loads(raw_line)
            except Exception as exc:
                raise RawLogFormatError(f"line {line_number}: invalid JSON: {exc}") from exc


def verify_raw_log(path: Path | str) -> RawLogAudit:
    errors: list[str] = []
    frames = controls = 0
    last_by_epoch: dict[int, int] = {}
    try:
        for row in iter_raw_records(path):
            if row.get("record_type") in {"WS_FRAME", "REST_FRAME"}:
                if row.get("record_type") == "WS_FRAME":
                    frames += 1
                try:
                    payload = raw_payload_bytes(row)
                    expected_sha = row.get("raw_sha256")
                    if expected_sha and hashlib.sha256(payload).hexdigest() != expected_sha:
                        errors.append(f"frame {frames}: SHA mismatch")
                    if row.get("record_type") == "WS_FRAME":
                        epoch, index = int(row["connection_epoch"]), int(row["local_frame_index"])
                        if index <= last_by_epoch.get(epoch, 0):
                            errors.append(f"frame {frames}: non-increasing local frame index")
                        last_by_epoch[epoch] = index
                except Exception as exc:
                    errors.append(f"frame {frames}: malformed frame metadata: {exc}")
            elif row.get("record_type") == "CONTROL":
                controls += 1
            else:
                errors.append("unknown record type")
    except Exception as exc:
        errors.append(str(exc))
    return RawLogAudit(not errors, frames, controls, tuple(errors))
