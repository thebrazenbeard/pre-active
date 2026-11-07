"""Opt-in host-keyed opportunity ingress for the existing observer→Volition route.

The reader, pinned HMAC keys and live goal-revision resolver belong to the host.
No producer, credential, observer or timer is provisioned by importing this module.
The read-only ``sample`` method does not admit or persist evidence; ``admit`` is
called inside the observer's existing SQLite transaction on an actual tick.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from typing import Any, Callable, Mapping

from .observers import Observation
from .store import Store


_ID = re.compile(r"[A-Za-z0-9:_-]{1,128}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_CLAIM_FIELDS = frozenset({
    "source_id", "opportunity_id", "goal_id", "goal_revision",
    "issued_at", "expires_at", "artifact_sha256",
})
_EVIDENCE_FIELDS = _CLAIM_FIELDS | {"hmac_sha256"}


class OpportunityAdmissionError(ValueError):
    """Untrusted, stale, unbound or conflicting opportunity evidence."""


class AuthenticatedOpportunityObserver:
    """Read an authenticated host-owned observation, never an executable order.

    HMAC pins a configured producer identity to exact bounded evidence. The
    integrating host must protect the signing key and goal revision oracle. The
    host must also ensure the reader really samples that producer; this module
    cannot authenticate an arbitrary real-world event on its own.
    """

    KIND = "authenticated_opportunity"

    def __init__(
        self,
        *,
        reader: Callable[[str], Mapping[str, Any]],
        pinned_keys: Mapping[str, bytes],
        current_goal_revision: Callable[[str], int],
        max_lifetime_seconds: float = 3600.0,
    ) -> None:
        if not callable(reader) or not callable(current_goal_revision):
            raise TypeError("host reader and live goal revision resolver required")
        if (
            isinstance(max_lifetime_seconds, bool)
            or not isinstance(max_lifetime_seconds, (int, float))
            or not math.isfinite(float(max_lifetime_seconds))
            or max_lifetime_seconds <= 0
        ):
            raise ValueError("max_lifetime_seconds must be positive and finite")
        keys: dict[str, bytes] = {}
        for source_id, key in pinned_keys.items():
            self._opaque(source_id, "source_id")
            if not isinstance(key, bytes) or len(key) < 32:
                raise ValueError("host-pinned HMAC key must have at least 32 bytes")
            keys[source_id] = key
        if not keys:
            raise ValueError("at least one host-pinned source key required")
        self._reader = reader
        self._keys = keys
        self._goal_revision = current_goal_revision
        self._max_lifetime = float(max_lifetime_seconds)

    @staticmethod
    def _opaque(value: object, name: str) -> str:
        if not isinstance(value, str) or _ID.fullmatch(value) is None:
            raise OpportunityAdmissionError(f"{name} must be a bounded opaque ID")
        return value

    @classmethod
    def _config(cls, config: Mapping[str, Any]) -> tuple[str, str]:
        if set(config) != {"source_id", "goal_id"}:
            raise OpportunityAdmissionError("source and goal binding required")
        return (
            cls._opaque(config["source_id"], "source_id"),
            cls._opaque(config["goal_id"], "goal_id"),
        )

    @classmethod
    def _normalize(cls, raw: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, Mapping) or set(raw) != _EVIDENCE_FIELDS:
            raise OpportunityAdmissionError("opportunity claim fields are not exact")
        claim = {key: raw[key] for key in _CLAIM_FIELDS}
        for key in ("source_id", "opportunity_id", "goal_id"):
            cls._opaque(claim[key], key)
        revision = claim["goal_revision"]
        if type(revision) is not int or not 0 <= revision <= 2**63 - 1:
            raise OpportunityAdmissionError("goal_revision must be a bounded integer")
        for key in ("issued_at", "expires_at"):
            value = claim[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise OpportunityAdmissionError(f"{key} must be a finite timestamp")
            try:
                finite = float(value)
            except OverflowError as exc:
                raise OpportunityAdmissionError(f"{key} must be finite") from exc
            if not math.isfinite(finite):
                raise OpportunityAdmissionError(f"{key} must be finite")
            # Preserve the signed JSON representation (100 != 100.0 for HMAC)
            # while using the finite float only for validation/comparison.
        for key in ("artifact_sha256", "hmac_sha256"):
            value = raw[key]
            if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
                raise OpportunityAdmissionError(f"{key} must be lowercase sha256 hex")
        if claim["expires_at"] <= claim["issued_at"]:
            raise OpportunityAdmissionError("opportunity must expire after issuance")
        claim["artifact_sha256"] = raw["artifact_sha256"]
        claim["hmac_sha256"] = raw["hmac_sha256"]
        return claim

    @staticmethod
    def _canonical(claim: Mapping[str, Any]) -> bytes:
        return json.dumps(
            {key: claim[key] for key in _CLAIM_FIELDS},
            sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")

    def _verify(self, config: Mapping[str, Any], raw: Mapping[str, Any]) -> dict[str, Any]:
        source_id, goal_id = self._config(config)
        claim = self._normalize(raw)
        if claim["source_id"] != source_id or claim["goal_id"] != goal_id:
            raise OpportunityAdmissionError("opportunity source or goal binding mismatch")
        key = self._keys.get(source_id)
        if key is None:
            raise OpportunityAdmissionError("source has no host-pinned verifier key")
        expected = hmac.new(key, self._canonical(claim), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, claim["hmac_sha256"]):
            raise OpportunityAdmissionError("opportunity authentication failed")
        if claim["expires_at"] - claim["issued_at"] > self._max_lifetime:
            raise OpportunityAdmissionError("opportunity lifetime exceeds host bound")
        return claim

    def sample(self, config: Mapping[str, Any]) -> Observation:
        source_id, _ = self._config(config)
        claim = self._verify(config, self._reader(source_id))
        digest = hashlib.sha256(self._canonical(claim)).hexdigest()
        return Observation(
            digest=digest,
            summary="signed opportunity sampled; admission pending",
            evidence=claim,
        )

    @staticmethod
    def public_observation(observation: Observation) -> Observation:
        """Never expose a reusable authentication tag in diagnostics or state."""
        return Observation(
            digest=observation.digest,
            summary=observation.summary,
            evidence={
                key: value for key, value in observation.evidence.items()
                if key != "hmac_sha256"
            },
        )

    def admit(
        self,
        *,
        config: Mapping[str, Any],
        observation: Observation,
        store: Store,
        now: float,
    ) -> bool:
        """Atomically bind the source/event ID; False is an exact prior replay.

        Called only on tick **within** ObserverManager's open transaction, so an
        enqueue/dispatch failure also rolls back this receipt.
        """
        if not store.connection.in_transaction:
            raise RuntimeError("opportunity admission requires observer transaction")
        claim = self._verify(config, observation.evidence)
        if not isinstance(now, (int, float)) or isinstance(now, bool) or not math.isfinite(float(now)):
            raise OpportunityAdmissionError("admission clock must be finite")
        if not claim["issued_at"] <= now <= claim["expires_at"]:
            raise OpportunityAdmissionError("opportunity is stale or not yet current")
        current_revision = self._goal_revision(claim["goal_id"])
        if type(current_revision) is not int or current_revision != claim["goal_revision"]:
            raise OpportunityAdmissionError("current trusted goal revision differs")
        digest = hashlib.sha256(self._canonical(claim)).hexdigest()
        if observation.digest != digest:
            raise OpportunityAdmissionError("observation digest is not bound to signed claim")
        store.connection.execute(
            "CREATE TABLE IF NOT EXISTS authenticated_opportunity_receipts ("
            "source_id TEXT NOT NULL, opportunity_id TEXT NOT NULL, "
            "claim_sha256 TEXT NOT NULL, admitted_at REAL NOT NULL, "
            "signal_event_id TEXT, "
            "PRIMARY KEY (source_id, opportunity_id))"
        )
        existing = store.connection.execute(
            "SELECT claim_sha256 FROM authenticated_opportunity_receipts "
            "WHERE source_id=? AND opportunity_id=?",
            (claim["source_id"], claim["opportunity_id"]),
        ).fetchone()
        if existing is not None:
            if existing["claim_sha256"] != digest:
                raise OpportunityAdmissionError("stable opportunity ID conflicts with prior evidence")
            return False
        store.connection.execute(
            "INSERT INTO authenticated_opportunity_receipts "
            "(source_id, opportunity_id, claim_sha256, admitted_at) "
            "VALUES (?, ?, ?, ?)",
            (claim["source_id"], claim["opportunity_id"], digest, now),
        )
        return True
