from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .book import BookReducer
from .raw_log import iter_raw_records, raw_payload_bytes


@dataclass(frozen=True)
class ReplayResult:
    canonical_hash: str
    input_frames: int
    parsed_frames: int
    pair_observations: int
    terminal_book_hash: str


def replay_records(path: Path | str, *, condition_id: str, up_token_id: str, down_token_id: str) -> ReplayResult:
    reducer = BookReducer(condition_id, up_token_id, down_token_id)
    input_frames = parsed = 0
    previous_epoch = None
    outputs: list[dict] = []
    for row in iter_raw_records(path):
        if row.get("record_type") != "WS_FRAME":
            if row.get("event_type") == "GAP_OPENED":
                reducer.open_gap(str(row.get("details") or {}))
            continue
        input_frames += 1
        epoch = int(row["connection_epoch"])
        if previous_epoch is None:
            reducer.begin_epoch(epoch)
        elif epoch != previous_epoch:
            reducer.begin_epoch(epoch)
        previous_epoch = epoch
        try:
            decoded = json.loads(raw_payload_bytes(row))
            messages = decoded if isinstance(decoded, list) else [decoded]
            if not all(isinstance(message, dict) for message in messages):
                raise ValueError("frame payload must contain object messages")
        except Exception:
            reducer.open_gap("replay_parse_error")
            continue
        parsed += 1
        for message in messages:
            observation = reducer.apply(
                message,
                frame_index=int(row["local_frame_index"]),
                received_monotonic_ns=int(row["received_monotonic_ns"]),
            )
            if observation:
                outputs.append({
                    "state": observation.state.value,
                    "frame_index": observation.frame_index,
                    "up_best_ask": str(observation.up_best_ask) if observation.up_best_ask is not None else None,
                    "down_best_ask": str(observation.down_best_ask) if observation.down_best_ask is not None else None,
                })
    terminal = reducer.canonical_hash()
    digest = hashlib.sha256(json.dumps({"terminal": terminal, "outputs": outputs}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return ReplayResult(digest, input_frames, parsed, len(outputs), terminal)
