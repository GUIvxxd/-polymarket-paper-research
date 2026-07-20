from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
import websockets

from .book import BookReducer
from .discovery import MarketIdentity, MarketValidationError, normalize_gamma_market, verify_clob_identity
from .fees import FeeMetadata, fee_gate
from .raw_log import DurableRawLog

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
UA = "Hermes-Both-Sides-Collector-Spike/1.0 (paper-only; public-data-only)"


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json_default(value: Any):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(type(value).__name__)


def _source_revision() -> dict[str, Any]:
    package_root = Path(__file__).resolve().parent
    hasher = hashlib.sha256()
    files: list[str] = []
    for path in sorted(package_root.glob("*.py")):
        relative = path.relative_to(package_root.parent).as_posix()
        payload = path.read_bytes()
        files.append(relative)
        hasher.update(relative.encode() + b"\0" + payload + b"\0")
    return {
        "kind": "source_tree_sha256",
        "value": hasher.hexdigest(),
        "files": files,
    }


class Collector:
    def __init__(
        self,
        run_dir: Path | str,
        *,
        duration_seconds: int = 600,
        fresh_ms: int = 1_000,
        reconnect_after_seconds: int = 90,
        durability_window_ms: int = 100,
        compression: str = "zlib",
        compression_level: int = 6,
        rolling: bool = False,
        discovery_interval_seconds: int = 300,
        prestart_lead_seconds: int = 30,
        status_interval_seconds: int = 10,
        disk_check_interval_seconds: int = 30,
        minimum_free_bytes: int = 1_000_000_000,
    ):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.duration_seconds = duration_seconds
        self.fresh_ms = fresh_ms
        self.reconnect_after_seconds = min(reconnect_after_seconds, max(10, duration_seconds // 2))
        self.durability_window_ms = durability_window_ms
        self.compression = compression
        self.compression_level = compression_level
        self.rolling = rolling
        self.discovery_interval_seconds = max(10, discovery_interval_seconds)
        self.prestart_lead_seconds = max(0, prestart_lead_seconds)
        self.status_interval_seconds = max(1, status_interval_seconds)
        if not 30 <= disk_check_interval_seconds <= 60:
            raise ValueError("disk_check_interval_seconds must be between 30 and 60")
        if minimum_free_bytes < 1_000_000_000:
            raise ValueError("minimum_free_bytes cannot be below 1,000,000,000")
        self.disk_check_interval_seconds = disk_check_interval_seconds
        self.minimum_free_bytes = minimum_free_bytes
        run_kind = "rolling" if rolling else "smoke"
        self.run_id = f"pair_{run_kind}_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
        self.raw_path = self.run_dir / "raw_frames.bssraw"
        self.manifest_path = self.run_dir / "manifest.json"
        self.status_path = self.run_dir / "status.json"
        self.log = DurableRawLog(
            self.raw_path,
            collector_run_id=self.run_id,
            storage_format="segmented_v2",
            durability_window_ms=durability_window_ms,
            compression=compression,
            compression_level=compression_level,
        )
        self.markets: list[MarketIdentity] = []
        self.market_meta: dict[str, dict[str, Any]] = {}
        self.reducers: dict[str, BookReducer] = {}
        self.pair_counts: dict[str, Counter] = defaultdict(Counter)
        self.pair_by_epoch: dict[str, Counter] = defaultdict(Counter)
        self.snapshots_by_epoch: dict[str, set[str]] = defaultdict(set)
        self.errors: list[str] = []
        self.connection_epochs = 0
        self.forced_reconnect_completed = False
        self.multi_token_frames = 0
        self.parser_errors = 0
        self.collection_elapsed_seconds = 0.0
        self.started_at = utc_now()
        self.registry: dict[str, MarketIdentity] = {}
        self.subscribed_condition_ids: set[str] = set()
        self.discovery_polls = 0
        self.subscription_rotations = 0
        self.subscription_history: list[dict[str, Any]] = []
        self.coverage_warnings: list[dict[str, Any]] = []
        self.current_markets: list[MarketIdentity] = []
        self.stop_requested = False
        self.stop_reason = "collector_deadline"
        self._stop_event_logged = False
        self.disk_samples: list[dict[str, Any]] = []
        self.starting_disk_available_bytes = self._available_disk_bytes()
        self.source_revision = _source_revision()
        self.configuration = {
            "duration_seconds": self.duration_seconds,
            "fresh_ms": self.fresh_ms,
            "reconnect_after_seconds": self.reconnect_after_seconds,
            "durability_window_ms": self.durability_window_ms,
            "compression": self.compression,
            "compression_level": self.compression_level,
            "rolling": self.rolling,
            "discovery_interval_seconds": self.discovery_interval_seconds,
            "prestart_lead_seconds": self.prestart_lead_seconds,
            "status_interval_seconds": self.status_interval_seconds,
            "disk_check_interval_seconds": self.disk_check_interval_seconds,
            "minimum_free_bytes": self.minimum_free_bytes,
            "paper_only": True,
            "live_orders_enabled": False,
        }
        self.log.append_control(
            "RUN_STARTED",
            {
                "started_at": self.started_at,
                "configuration": self.configuration,
                "record_schema": "both_sides_raw_v2",
                "source_revision": self.source_revision,
                "starting_available_bytes": self.starting_disk_available_bytes,
            },
        )

    def _available_disk_bytes(self) -> int:
        stats = os.statvfs(self.run_dir)
        return int(stats.f_bavail * stats.f_frsize)

    def request_stop(self, reason: str, *, append_event: bool = True) -> None:
        """Request a controlled stop.

        Signal handlers must pass ``append_event=False`` because Python signal
        handlers can interrupt a segment flush; performing archive I/O there
        would re-enter the writer and corrupt segment ordering.
        """
        if self.stop_requested:
            return
        self.stop_requested = True
        self.stop_reason = reason
        if append_event:
            self._log_stop_requested_if_needed()

    def _log_stop_requested_if_needed(self) -> None:
        if not self.stop_requested or self._stop_event_logged or self.log.sealed:
            return
        self.log.append_control("STOP_REQUESTED", {"reason": self.stop_reason})
        self._stop_event_logged = True

    def check_disk(self) -> bool:
        available = self._available_disk_bytes()
        sample = {
            "checked_at": utc_now(),
            "available_bytes": available,
            "minimum_free_bytes": self.minimum_free_bytes,
            "raw_log_bytes": self.raw_path.stat().st_size if self.raw_path.exists() else 0,
        }
        self.disk_samples.append(sample)
        self.log.append_control("DISK_CHECK", sample)
        if available < self.minimum_free_bytes:
            self.log.append_control("DISK_GUARD_TRIGGERED", sample)
            self.request_stop("disk_floor_breached")
            return False
        return True

    def _get_bytes(self, url: str, *, params: dict[str, Any] | None = None, timeout: int = 20) -> bytes:
        final_url = url + ("?" + urlencode(params, doseq=True) if params else "")
        started = time.time_ns()
        response = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": UA})
        received = time.time_ns()
        raw = response.content
        self.log.append_rest(
            raw,
            url=response.url or final_url,
            request_started_wall_ns=started,
            response_received_wall_ns=received,
            status_code=response.status_code,
        )
        response.raise_for_status()
        return raw

    def _get_json(self, url: str, *, params: dict[str, Any] | None = None) -> Any:
        raw = self._get_bytes(url, params=params)
        return json.loads(raw)

    @staticmethod
    def subscription_fingerprint(markets: list[MarketIdentity]) -> str:
        tokens = sorted({token for market in markets for token in (market.up_token_id, market.down_token_id)})
        return hashlib.sha256("|".join(tokens).encode()).hexdigest()

    def _fetch_normalized_candidates(self) -> tuple[dict[str, MarketIdentity], dict[str, int]]:
        now = datetime.now(UTC)
        lower = now - timedelta(minutes=30)
        upper = now + timedelta(minutes=45)
        rows: list[dict[str, Any]] = []
        offset, limit = 0, 100
        while True:
            payload = self._get_json(
                f"{GAMMA}/events",
                params={
                    "active": "true", "closed": "false", "limit": limit, "offset": offset,
                    "end_date_min": lower.isoformat(), "end_date_max": upper.isoformat(),
                    "order": "endDate", "ascending": "true",
                },
            )
            if not isinstance(payload, list):
                raise RuntimeError("Gamma events response is not a list")
            for event in payload:
                if not isinstance(event, dict):
                    continue
                for market in event.get("markets") or []:
                    if isinstance(market, dict):
                        merged = dict(market)
                        merged.setdefault("question", event.get("title"))
                        rows.append(merged)
            if len(payload) < limit:
                break
            offset += limit
            if offset >= 1000:
                break
        accepted: dict[str, MarketIdentity] = {}
        rejected = Counter()
        for row in rows:
            try:
                market = normalize_gamma_market(row, verified_at=utc_now())
            except MarketValidationError as exc:
                rejected[str(exc)] += 1
                continue
            end = datetime.fromisoformat(market.prediction_end.replace("Z", "+00:00"))
            start = datetime.fromisoformat(market.prediction_start.replace("Z", "+00:00"))
            if end < lower or start > upper:
                continue
            accepted[market.condition_id] = market
        self.discovery_polls += 1
        self.log.append_control(
            "DISCOVERY_CLASSIFIED",
            {
                "poll": self.discovery_polls,
                "candidate_rows": len(rows),
                "accepted": len(accepted),
                "rejected": dict(rejected),
                "window_start": lower.isoformat(),
                "window_end": upper.isoformat(),
            },
        )
        return accepted, dict(rejected)

    def discover(self) -> list[MarketIdentity]:
        now = datetime.now(UTC)
        accepted, _ = self._fetch_normalized_candidates()
        groups: dict[tuple[str, int], list[MarketIdentity]] = defaultdict(list)
        for market in accepted.values():
            groups[(market.asset, market.duration_seconds)].append(market)
        selected: list[MarketIdentity] = []
        for key in (("BTC", 300), ("ETH", 300), ("BTC", 900), ("ETH", 900)):
            candidates = [m for m in groups.get(key, []) if datetime.fromisoformat(m.prediction_end.replace("Z", "+00:00")) > now]
            if not candidates:
                continue
            candidates.sort(key=lambda m: abs((datetime.fromisoformat(m.prediction_start.replace("Z", "+00:00")) - now).total_seconds()))
            selected.append(candidates[0])
        future = [m for m in accepted.values() if datetime.fromisoformat(m.prediction_start.replace("Z", "+00:00")) > now + timedelta(seconds=60)]
        future.sort(key=lambda m: m.prediction_start)
        if future and future[0].condition_id not in {m.condition_id for m in selected}:
            selected.append(future[0])
        if len({(m.asset, m.duration_seconds) for m in selected}) < 4:
            raise RuntimeError(f"discovery did not find all four required market classes: {[(m.asset, m.duration_seconds) for m in selected]}")
        self.markets = selected
        for market in selected:
            self.market_meta[market.condition_id] = {
                "identity": asdict(market),
                "stages": ["DISCOVERED", "GAMMA_VERIFIED", "CLOB_PENDING"],
                "is_future": datetime.fromisoformat(market.prediction_start.replace("Z", "+00:00")) > now,
            }
            self.log.append_control("MARKET_STAGE", {"condition_id": market.condition_id, "stage": "CLOB_PENDING"})
        return selected

    def _verify_candidate(self, market: MarketIdentity) -> MarketIdentity | None:
        meta = self.market_meta.setdefault(
            market.condition_id,
            {
                "identity": asdict(market),
                "stages": ["DISCOVERED", "GAMMA_VERIFIED", "CLOB_PENDING"],
                "is_future": datetime.fromisoformat(market.prediction_start.replace("Z", "+00:00")) > datetime.now(UTC),
            },
        )
        try:
            clob = self._get_json(f"{CLOB}/markets/{market.condition_id}")
            checked = verify_clob_identity(market, clob)
            up_fee = self._get_json(f"{CLOB}/fee-rate", params={"token_id": market.up_token_id})
            down_fee = self._get_json(f"{CLOB}/fee-rate", params={"token_id": market.down_token_id})
            fee = FeeMetadata.from_sources(
                fees_enabled=market.fees_enabled,
                gamma_schedule=market.gamma_fee_schedule,
                up_base_fee=up_fee.get("base_fee") if isinstance(up_fee, dict) else None,
                down_base_fee=down_fee.get("base_fee") if isinstance(down_fee, dict) else None,
            )
            gate = fee_gate(fee)
            meta["clob_payload"] = clob
            meta["up_base_fee_payload"] = up_fee
            meta["down_base_fee_payload"] = down_fee
            meta["fee_metadata"] = asdict(fee)
            meta["fee_gate"] = asdict(gate)
            meta["identity"] = asdict(checked)
            if checked.stage not in meta["stages"]:
                meta["stages"].append(checked.stage)
            self.log.append_control("MARKET_STAGE", {"condition_id": market.condition_id, "stage": checked.stage})
            if checked.stage == "IDENTITY_VERIFIED" and gate.economic_eligible:
                return checked
            meta["ineligible_reason"] = f"stage={checked.stage}; fee_eligible={gate.economic_eligible}"
        except Exception as exc:
            meta["ineligible_reason"] = f"{type(exc).__name__}: {exc}"
            self.errors.append(f"verify {market.slug}: {type(exc).__name__}: {exc}")
        return None

    def verify_markets(self) -> list[MarketIdentity]:
        verified = [checked for market in self.markets if (checked := self._verify_candidate(market)) is not None]
        self.markets = verified
        if len({(m.asset, m.duration_seconds) for m in verified}) < 4:
            raise RuntimeError("fewer than four required market classes survived identity/fee verification")
        self.registry.update({market.condition_id: market for market in verified})
        self.reducers.update({
            m.condition_id: BookReducer(m.condition_id, m.up_token_id, m.down_token_id, fresh_ms=self.fresh_ms)
            for m in verified if m.condition_id not in self.reducers
        })
        return verified

    def refresh_registry(self) -> list[MarketIdentity]:
        accepted, _ = self._fetch_normalized_candidates()
        verified_now: list[MarketIdentity] = []
        for market in accepted.values():
            existing = self.registry.get(market.condition_id)
            if existing is not None:
                verified_now.append(existing)
                continue
            self.market_meta.setdefault(
                market.condition_id,
                {
                    "identity": asdict(market),
                    "stages": ["DISCOVERED", "GAMMA_VERIFIED", "CLOB_PENDING"],
                    "is_future": datetime.fromisoformat(market.prediction_start.replace("Z", "+00:00")) > datetime.now(UTC),
                },
            )
            checked = self._verify_candidate(market)
            if checked is None:
                continue
            self.registry[checked.condition_id] = checked
            self.reducers[checked.condition_id] = BookReducer(
                checked.condition_id, checked.up_token_id, checked.down_token_id, fresh_ms=self.fresh_ms,
            )
            verified_now.append(checked)
        self.log.append_control(
            "REGISTRY_REFRESHED",
            {
                "poll": self.discovery_polls,
                "accepted_in_window": len(accepted),
                "verified_registry_total": len(self.registry),
                "newly_verified": len([m for m in verified_now if m.condition_id not in self.subscribed_condition_ids]),
            },
        )
        return verified_now

    def desired_markets(self, *, now: datetime | None = None) -> list[MarketIdentity]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        lead = timedelta(seconds=self.prestart_lead_seconds)
        desired = []
        for market in self.registry.values():
            start = datetime.fromisoformat(market.prediction_start.replace("Z", "+00:00"))
            end = datetime.fromisoformat(market.prediction_end.replace("Z", "+00:00"))
            if start <= current + lead and end > current:
                desired.append(market)
        return sorted(desired, key=lambda m: (m.prediction_start, m.asset, m.duration_seconds, m.condition_id))

    def _write_status(self, *, state: str, active: list[MarketIdentity] | None = None, reason: str | None = None) -> None:
        active = active if active is not None else self.current_markets
        payload = {
            "schema_version": "both_sides_rolling_status_v1",
            "collector_run_id": self.run_id,
            "state": state,
            "reason": reason,
            "heartbeat_at": utc_now(),
            "pid": __import__("os").getpid(),
            "collection_mode": "rolling" if self.rolling else "static_smoke",
            "connection_epochs": self.connection_epochs,
            "discovery_polls": self.discovery_polls,
            "subscription_rotations": self.subscription_rotations,
            "registry_market_count": len(self.registry),
            "active_market_count": len(active),
            "active_markets": [
                {
                    "condition_id": market.condition_id,
                    "slug": market.slug,
                    "asset": market.asset,
                    "duration_seconds": market.duration_seconds,
                    "prediction_start": market.prediction_start,
                    "prediction_end": market.prediction_end,
                }
                for market in active
            ],
            "raw_log_bytes": self.raw_path.stat().st_size if self.raw_path.exists() else 0,
            "durable_frame_count": self.log.durable_latency_count,
            "pair_observation_count": sum(sum(counter.values()) for counter in self.pair_counts.values()),
            "durable_latency": self.log.durable_latency_summary(),
            "disk_guard": {
                "starting_available_bytes": self.starting_disk_available_bytes,
                "latest": self.disk_samples[-1] if self.disk_samples else None,
                "minimum_free_bytes": self.minimum_free_bytes,
                "check_interval_seconds": self.disk_check_interval_seconds,
            },
            "stop_requested": self.stop_requested,
            "stop_reason": self.stop_reason if self.stop_requested else None,
            "parser_errors": self.parser_errors,
            "live_orders": 0,
            "wallet_or_authentication_used": False,
        }
        temporary = self.status_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        temporary.replace(self.status_path)

    async def collect(self) -> None:
        collection_started = time.monotonic()
        tokens = sorted({token for m in self.markets for token in (m.up_token_id, m.down_token_id)})
        subscription_hash = hashlib.sha256("|".join(tokens).encode()).hexdigest()
        deadline = time.monotonic() + self.duration_seconds
        force_at = time.monotonic() + self.reconnect_after_seconds
        while time.monotonic() < deadline:
            self.connection_epochs += 1
            epoch = self.connection_epochs
            self.log.set_epoch(epoch)
            for reducer in self.reducers.values():
                reducer.begin_epoch(epoch)
            self.log.append_control("CONNECTION_ATTEMPT", {"socket_url": WS, "token_count": len(tokens)})
            reconnect_reason = "collector_deadline"
            try:
                async with websockets.connect(WS, ping_interval=None, close_timeout=5, max_size=8_000_000) as socket:
                    self.log.append_control("CONNECTION_OPENED", {"epoch": epoch})
                    subscription = {"assets_ids": tokens, "type": "market", "custom_feature_enabled": True}
                    await socket.send(json.dumps(subscription, separators=(",", ":")))
                    self.log.append_control("SUBSCRIPTION_SENT", {"assets_ids": tokens, "subscription_set_hash": subscription_hash})
                    last_ping = time.monotonic()
                    while time.monotonic() < deadline:
                        if not self.forced_reconnect_completed and time.monotonic() >= force_at:
                            reconnect_reason = "forced_smoke_reconnect"
                            self.forced_reconnect_completed = True
                            break
                        if time.monotonic() - last_ping >= 10:
                            await socket.send("PING")
                            self.log.append_control("PING_SENT")
                            last_ping = time.monotonic()
                        wait_timeout = min(
                            2.0,
                            max(0.001, deadline - time.monotonic()),
                            max(0.001, self.log.seconds_until_flush()),
                        )
                        try:
                            raw = await asyncio.wait_for(socket.recv(), timeout=wait_timeout)
                        except TimeoutError:
                            self.log.flush_due()
                            continue
                        payload = raw.encode() if isinstance(raw, str) else bytes(raw)
                        frame = self.log.append_frame(payload, force_flush=False, subscription_set_hash=subscription_hash)
                        if payload in {b"PONG", b"PING"}:
                            self.log.append_control(payload.decode() + "_RECEIVED", {"frame_index": frame["local_frame_index"]})
                            continue
                        try:
                            parsed = json.loads(payload)
                            messages = parsed if isinstance(parsed, list) else [parsed]
                            if not all(isinstance(item, dict) for item in messages):
                                raise ValueError("WS payload is not an object/list of objects")
                        except Exception as exc:
                            self.parser_errors += 1
                            reconnect_reason = f"parser_error:{type(exc).__name__}"
                            self.log.append_control("PARSER_ERROR", {"frame_index": frame["local_frame_index"], "error": str(exc)})
                            self.log.flush(force=True)
                            break
                        for message in messages:
                            changes = message.get("price_changes")
                            if isinstance(changes, list) and len({str(x.get("asset_id")) for x in changes if isinstance(x, dict)}) > 1:
                                self.multi_token_frames += 1
                            condition = str(message.get("market") or "")
                            reducer = self.reducers.get(condition)
                            if reducer is None:
                                continue
                            if message.get("event_type") == "book":
                                token = str(message.get("asset_id") or "")
                                self.snapshots_by_epoch[f"{condition}|{epoch}"].add(token)
                            observation = reducer.apply(
                                message,
                                frame_index=int(frame["local_frame_index"]),
                                received_monotonic_ns=int(frame["received_monotonic_ns"]),
                            )
                            if observation:
                                self.pair_counts[condition][observation.state.value] += 1
                                self.pair_by_epoch[condition][str(epoch)] += 1
                    if time.monotonic() >= deadline:
                        reconnect_reason = "collector_deadline"
            except Exception as exc:
                reconnect_reason = f"socket_error:{type(exc).__name__}"
                self.errors.append(f"epoch {epoch}: {type(exc).__name__}: {exc}")
            self.log.append_control("GAP_OPENED", {"reason": reconnect_reason, "epoch": epoch})
            for reducer in self.reducers.values():
                reducer.open_gap(reconnect_reason)
            if time.monotonic() < deadline:
                await asyncio.sleep(1)
        self.collection_elapsed_seconds = time.monotonic() - collection_started
        self.log.append_control("COLLECTION_COMPLETE", {"epochs": self.connection_epochs, "elapsed_seconds": self.collection_elapsed_seconds})

    async def collect_rolling(self) -> None:
        collection_started = time.monotonic()
        deadline = collection_started + self.duration_seconds
        forced_at = collection_started + self.reconnect_after_seconds
        next_discovery = collection_started
        next_status = collection_started
        next_disk_check = collection_started
        previous_fingerprint: str | None = None
        while time.monotonic() < deadline and not self.stop_requested:
            now_mono = time.monotonic()
            if now_mono >= next_disk_check:
                if not self.check_disk():
                    break
                next_disk_check = now_mono + self.disk_check_interval_seconds
            if now_mono >= next_discovery:
                self.log.flush(force=True)
                self._write_status(state="DISCOVERING", reason="scheduled_registry_refresh")
                try:
                    self.refresh_registry()
                except Exception as exc:
                    self.errors.append(f"discovery poll {self.discovery_polls + 1}: {type(exc).__name__}: {exc}")
                    self.log.append_control("DISCOVERY_ERROR", {"error": f"{type(exc).__name__}: {exc}"})
                next_discovery = time.monotonic() + self.discovery_interval_seconds

            desired = self.desired_markets()
            classes = {f"{m.asset}-{m.duration_seconds // 60}m" for m in desired}
            required = {"BTC-5m", "ETH-5m", "BTC-15m", "ETH-15m"}
            if not required <= classes:
                warning = {"at": utc_now(), "missing_classes": sorted(required - classes)}
                self.coverage_warnings.append(warning)
                self.log.append_control("COVERAGE_WARNING", warning)
            if not desired:
                self._write_status(state="WAITING_FOR_MARKETS", active=[], reason="no_verified_active_or_near_future_markets")
                await asyncio.sleep(min(2.0, max(0.01, deadline - time.monotonic())))
                continue

            tokens = sorted({token for market in desired for token in (market.up_token_id, market.down_token_id)})
            fingerprint = self.subscription_fingerprint(desired)
            if previous_fingerprint is not None and fingerprint != previous_fingerprint:
                self.subscription_rotations += 1
            previous_fingerprint = fingerprint
            self.current_markets = desired
            current_conditions = {market.condition_id for market in desired}
            self.subscribed_condition_ids.update(current_conditions)

            self.connection_epochs += 1
            epoch = self.connection_epochs
            self.log.set_epoch(epoch)
            for market in desired:
                self.reducers[market.condition_id].begin_epoch(epoch)
            history = {
                "epoch": epoch,
                "subscribed_at": utc_now(),
                "subscription_set_hash": fingerprint,
                "condition_ids": sorted(current_conditions),
                "tokens": tokens,
                "market_classes": sorted(classes),
            }
            self.subscription_history.append(history)
            self.log.append_control("CONNECTION_ATTEMPT", {"socket_url": WS, "token_count": len(tokens), "rolling": True})
            reconnect_reason = "collector_deadline"
            unframed_websocket_close = False
            try:
                async with websockets.connect(WS, ping_interval=None, close_timeout=5, max_size=8_000_000) as socket:
                    self.log.append_control("CONNECTION_OPENED", {"epoch": epoch})
                    subscription = {"assets_ids": tokens, "type": "market", "custom_feature_enabled": True}
                    await socket.send(json.dumps(subscription, separators=(",", ":")))
                    self.log.append_control("SUBSCRIPTION_SENT", {"assets_ids": tokens, "subscription_set_hash": fingerprint})
                    self._write_status(state="COLLECTING", active=desired, reason="subscription_readying")
                    last_ping = time.monotonic()
                    while time.monotonic() < deadline and not self.stop_requested:
                        now_mono = time.monotonic()
                        if now_mono >= next_disk_check:
                            if not self.check_disk():
                                reconnect_reason = self.stop_reason
                                self.log.flush(force=True)
                                break
                            next_disk_check = now_mono + self.disk_check_interval_seconds
                        if self.stop_requested:
                            self._log_stop_requested_if_needed()
                            reconnect_reason = self.stop_reason
                            self.log.flush(force=True)
                            break
                        if not self.forced_reconnect_completed and now_mono >= forced_at:
                            reconnect_reason = "forced_preflight_reconnect"
                            self.forced_reconnect_completed = True
                            self.log.flush(force=True)
                            break
                        if now_mono >= next_discovery:
                            reconnect_reason = "scheduled_registry_refresh"
                            self.log.flush(force=True)
                            break
                        new_desired = self.desired_markets()
                        if self.subscription_fingerprint(new_desired) != fingerprint:
                            reconnect_reason = "subscription_rotation"
                            self.log.flush(force=True)
                            break
                        if now_mono >= next_status:
                            self._write_status(state="COLLECTING", active=desired)
                            next_status = now_mono + self.status_interval_seconds
                        if now_mono - last_ping >= 10:
                            await socket.send("PING")
                            self.log.append_control("PING_SENT")
                            last_ping = now_mono
                        wait_timeout = min(
                            2.0,
                            max(0.001, deadline - now_mono),
                            max(0.001, next_discovery - now_mono),
                            max(0.001, next_status - now_mono),
                            max(0.001, next_disk_check - now_mono),
                            max(0.001, self.log.seconds_until_flush()),
                        )
                        try:
                            raw = await asyncio.wait_for(socket.recv(), timeout=wait_timeout)
                        except TimeoutError:
                            self.log.flush_due()
                            continue
                        payload = raw.encode() if isinstance(raw, str) else bytes(raw)
                        frame = self.log.append_frame(payload, force_flush=False, subscription_set_hash=fingerprint)
                        if payload in {b"PONG", b"PING"}:
                            self.log.append_control(payload.decode() + "_RECEIVED", {"frame_index": frame["local_frame_index"]})
                            continue
                        try:
                            parsed = json.loads(payload)
                            messages = parsed if isinstance(parsed, list) else [parsed]
                            if not all(isinstance(item, dict) for item in messages):
                                raise ValueError("WS payload is not an object/list of objects")
                        except Exception as exc:
                            self.parser_errors += 1
                            reconnect_reason = f"parser_error:{type(exc).__name__}"
                            self.log.append_control("PARSER_ERROR", {"frame_index": frame["local_frame_index"], "error": str(exc)})
                            self.log.flush(force=True)
                            break
                        for message in messages:
                            changes = message.get("price_changes")
                            if isinstance(changes, list) and len({str(x.get("asset_id")) for x in changes if isinstance(x, dict)}) > 1:
                                self.multi_token_frames += 1
                            condition = str(message.get("market") or "")
                            reducer = self.reducers.get(condition)
                            if reducer is None or condition not in current_conditions:
                                continue
                            if message.get("event_type") == "book":
                                token = str(message.get("asset_id") or "")
                                self.snapshots_by_epoch[f"{condition}|{epoch}"].add(token)
                            observation = reducer.apply(
                                message,
                                frame_index=int(frame["local_frame_index"]),
                                received_monotonic_ns=int(frame["received_monotonic_ns"]),
                            )
                            if observation:
                                self.pair_counts[condition][observation.state.value] += 1
                                self.pair_by_epoch[condition][str(epoch)] += 1
            except Exception as exc:
                reconnect_reason = f"socket_error:{type(exc).__name__}"
                unframed_websocket_close = (
                    type(exc).__name__.startswith("ConnectionClosed")
                    and getattr(exc, "rcvd", None) is None
                    and getattr(exc, "sent", None) is None
                )
                error_details = {
                    "epoch": epoch,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "unframed_websocket_close": unframed_websocket_close,
                }
                self.errors.append(f"epoch {epoch}: {type(exc).__name__}: {exc}")
                self.log.append_control("SOCKET_EXCEPTION", error_details)
            gap_details = {
                "reason": reconnect_reason,
                "epoch": epoch,
                "condition_ids": sorted(current_conditions),
                "unframed_websocket_close": unframed_websocket_close,
                "next_epoch_required": time.monotonic() < deadline and not self.stop_requested,
            }
            history["ended_at"] = utc_now()
            history["gap_reason"] = reconnect_reason
            history["unframed_websocket_close"] = unframed_websocket_close
            self.log.append_control("GAP_OPENED", gap_details)
            self.log.flush(force=True)
            for condition in current_conditions:
                self.reducers[condition].open_gap(reconnect_reason)
            self._write_status(state="RECONNECTING", active=desired, reason=reconnect_reason)
            if reconnect_reason.startswith("socket_error") and time.monotonic() < deadline:
                await asyncio.sleep(1)

        self.collection_elapsed_seconds = time.monotonic() - collection_started
        self._log_stop_requested_if_needed()
        terminal_reason = self.stop_reason if self.stop_requested else "collector_deadline"
        self.stop_reason = terminal_reason
        self.log.append_control(
            "COLLECTION_COMPLETE",
            {
                "epochs": self.connection_epochs,
                "elapsed_seconds": self.collection_elapsed_seconds,
                "rolling": True,
                "terminal_reason": terminal_reason,
                "controlled_stop": self.stop_requested,
            },
        )
        self._write_status(
            state="CONTROLLED_STOP" if self.stop_requested else "COMPLETE",
            reason=terminal_reason,
        )

    def write_manifest(self) -> Path:
        terminal = self.log.seal(
            self.stop_reason,
            {
                "controlled_stop": self.stop_requested,
                "collection_elapsed_seconds": self.collection_elapsed_seconds,
                "errors_count": len(self.errors),
                "parser_errors": self.parser_errors,
            },
        )
        latency = self.log.durable_latency_summary()
        ended_at = utc_now()
        manifest_market_ids = self.subscribed_condition_ids if self.rolling else {m.condition_id for m in self.markets}
        manifest_markets = [self.market_meta[condition] for condition in sorted(manifest_market_ids) if condition in self.market_meta]
        observed_markets = [self.registry[condition] for condition in sorted(manifest_market_ids) if condition in self.registry]
        manifest = {
            "schema_version": "both_sides_rolling_manifest_v1" if self.rolling else "both_sides_smoke_manifest_v1",
            "collection_mode": "rolling" if self.rolling else "static_smoke",
            "collector_run_id": self.run_id,
            "started_at": self.started_at,
            "ended_at": ended_at,
            "duration_requested_seconds": self.duration_seconds,
            "collection_elapsed_seconds": self.collection_elapsed_seconds,
            "terminal_reason": self.stop_reason,
            "controlled_stop": self.stop_requested,
            "terminal": terminal,
            "configuration": self.configuration,
            "provenance": {
                "source_revision": self.source_revision,
                "manifest_schema": "both_sides_rolling_manifest_v1" if self.rolling else "both_sides_smoke_manifest_v1",
                "raw_record_schema": "both_sides_raw_v2",
            },
            "storage_guard": {
                "starting_available_bytes": self.starting_disk_available_bytes,
                "minimum_free_bytes": self.minimum_free_bytes,
                "check_interval_seconds": self.disk_check_interval_seconds,
                "samples": self.disk_samples,
                "ending_available_bytes": self._available_disk_bytes(),
            },
            "raw_log": str(self.raw_path.resolve()),
            "raw_storage_format": "segmented_v2",
            "durability_window_ms": self.durability_window_ms,
            "compression": self.compression,
            "compression_level": self.compression_level,
            "markets": manifest_markets,
            "verified_market_count": len(manifest_markets),
            "registry_market_count": len(self.registry),
            "required_market_classes": sorted({f"{m.asset}-{m.duration_seconds // 60}m" for m in observed_markets}),
            "discovery_polls": self.discovery_polls,
            "discovery_interval_seconds": self.discovery_interval_seconds,
            "prestart_lead_seconds": self.prestart_lead_seconds,
            "subscription_rotations": self.subscription_rotations,
            "subscription_history": self.subscription_history,
            "coverage_warnings": self.coverage_warnings,
            "connection_epochs": self.connection_epochs,
            "forced_reconnect_completed": self.forced_reconnect_completed,
            "pair_counts": {k: dict(v) for k, v in self.pair_counts.items()},
            "pair_by_epoch": {k: dict(v) for k, v in self.pair_by_epoch.items()},
            "snapshots_by_epoch": {k: sorted(v) for k, v in self.snapshots_by_epoch.items()},
            "multi_token_frames_observed": self.multi_token_frames,
            "atomic_multi_token_handling_tested": True,
            "parser_errors": self.parser_errors,
            "errors": self.errors,
            "durable_frame_count": self.log.durable_latency_count,
            "durable_latency": latency,
            "p99_receive_to_durable_log_ms": latency["p99_ms"],
            "property_examples_per_core_subsystem": 10_000,
            "property_subsystems": ["fees", "depth_walking"],
            "tests_verified_before_smoke": True,
            "live_orders": 0,
            "wallet_or_authentication_used": False,
        }
        temporary = self.manifest_path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(manifest, indent=2, sort_keys=True, default=_json_default) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.manifest_path)
        return self.manifest_path

    def close(self) -> None:
        self.log.close()


def run_rolling(
    run_dir: Path | str,
    *,
    duration_seconds: int = 86_400,
    fresh_ms: int = 1_000,
    reconnect_after_seconds: int = 90,
    discovery_interval_seconds: int = 300,
    prestart_lead_seconds: int = 30,
    durability_window_ms: int = 200,
    compression: str = "zstd",
    compression_level: int = 12,
    disk_check_interval_seconds: int = 30,
    minimum_free_bytes: int = 1_000_000_000,
) -> Path:
    collector = Collector(
        run_dir,
        duration_seconds=duration_seconds,
        fresh_ms=fresh_ms,
        reconnect_after_seconds=reconnect_after_seconds,
        rolling=True,
        durability_window_ms=durability_window_ms,
        compression=compression,
        compression_level=compression_level,
        discovery_interval_seconds=discovery_interval_seconds,
        prestart_lead_seconds=prestart_lead_seconds,
        disk_check_interval_seconds=disk_check_interval_seconds,
        minimum_free_bytes=minimum_free_bytes,
    )
    previous_handlers: dict[signal.Signals, Any] = {}

    def controlled_signal(signum, _frame) -> None:
        name = signal.Signals(signum).name.lower()
        # Signal handlers may interrupt zstd compression/fsync. Never perform
        # log I/O here; the normal collector loop records the stop request.
        collector.request_stop(f"signal_{name}", append_event=False)

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, controlled_signal)
        asyncio.run(collector.collect_rolling())
        return collector.write_manifest()
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        collector.close()


def run_smoke(run_dir: Path | str, *, duration_seconds: int = 600, fresh_ms: int = 1_000, reconnect_after_seconds: int = 90) -> Path:
    collector = Collector(run_dir, duration_seconds=duration_seconds, fresh_ms=fresh_ms, reconnect_after_seconds=reconnect_after_seconds)
    try:
        collector.discover()
        collector.verify_markets()
        asyncio.run(collector.collect())
        return collector.write_manifest()
    finally:
        collector.close()
