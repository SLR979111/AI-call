from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

ARTIFACT_KEYS = ("flow", "prompt", "knowledge", "model", "config")


class RegistryError(Exception):
    pass


@dataclass
class Release:
    release_id: str
    status: str
    artifacts: dict[str, str]
    metrics: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    parent_release_id: str | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "release_id": self.release_id,
            "status": self.status,
            "artifacts": self.artifacts,
            "metrics": self.metrics,
            "created_at": self.created_at,
            "parent_release_id": self.parent_release_id,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Release":
        return cls(
            release_id=data["release_id"],
            status=data.get("status", "candidate"),
            artifacts=dict(data.get("artifacts", {})),
            metrics=dict(data.get("metrics", {})),
            created_at=data.get("created_at", ""),
            parent_release_id=data.get("parent_release_id"),
            notes=data.get("notes", ""),
        )


class Registry:
    """File-backed release registry.

    candidate -> (gate) -> production -> previous -> archived.
    Only the architect-facing API can promote/rollback; eval only writes candidates.
    """

    def __init__(self, data_dir: Path) -> None:
        self.root = Path(data_dir) / "registry"
        self.releases_dir = self.root / "releases"
        self.state_path = self.root / "state.json"
        self.releases_dir.mkdir(parents=True, exist_ok=True)
        self._state = self._load_state()
        if not self._state.get("production"):
            self._seed_default()

    # ---- persistence ----
    def _now(self) -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def _load_state(self) -> dict[str, Any]:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        return {"production": None, "previous": None}

    def _write_state(self) -> None:
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _manifest_path(self, release_id: str) -> Path:
        return self.releases_dir / release_id / "manifest.json"

    def _write_manifest(self, release: Release) -> None:
        path = self._manifest_path(release.release_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(release.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def _read_manifest(self, release_id: str) -> Release:
        path = self._manifest_path(release_id)
        if not path.exists():
            raise RegistryError(f"release not found: {release_id}")
        return Release.from_dict(json.loads(path.read_text(encoding="utf-8")))

    # ---- default ----
    def _seed_default(self) -> None:
        release = Release(
            release_id="rel_dev_001",
            status="production",
            artifacts={
                "flow": "service_outbound@1.0.0",
                "prompt": "agent_system@1",
                "knowledge": "faq@dev",
                "model": "fake@dev",
                "config": "runtime@1",
            },
            metrics={"note": "seed"},
            created_at=self._now(),
            notes="seed release",
        )
        self._write_manifest(release)
        self._state = {"production": release.release_id, "previous": None}
        self._write_state()

    # ---- public ----
    def validate_artifacts(self, artifacts: dict[str, str]) -> None:
        missing = [key for key in ARTIFACT_KEYS if not artifacts.get(key)]
        if missing:
            raise RegistryError(f"missing artifacts: {missing}")

    def create_candidate(self, artifacts: dict[str, str], metrics: dict[str, Any] | None = None,
                         notes: str = "") -> dict[str, Any]:
        self.validate_artifacts(artifacts)
        release_id = f"rel_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}"
        production = self._state.get("production")
        release = Release(
            release_id=release_id,
            status="candidate",
            artifacts=dict(artifacts),
            metrics=metrics or {},
            created_at=self._now(),
            parent_release_id=production,
            notes=notes,
        )
        self._write_manifest(release)
        return release.to_dict()

    def promote(self, release_id: str, gate: dict[str, Any] | None = None) -> dict[str, Any]:
        release = self._read_manifest(release_id)
        if release.status not in {"candidate", "previous"}:
            raise RegistryError(f"cannot promote release in status {release.status}")
        if gate is not None and gate.get("ok") is False:
            raise RegistryError(f"gate rejected: {gate.get('reasons', gate)}")
        old_production = self._state.get("production")
        if old_production and old_production != release_id:
            try:
                old = self._read_manifest(old_production)
                old.status = "previous"
                self._write_manifest(old)
                self._state["previous"] = old_production
            except RegistryError:
                pass
        release.status = "production"
        self._write_manifest(release)
        self._state["production"] = release_id
        self._write_state()
        return release.to_dict()

    def rollback(self) -> dict[str, Any]:
        previous = self._state.get("previous")
        if not previous:
            raise RegistryError("no previous release to roll back to")
        current = self._state.get("production")
        target = self._read_manifest(previous)
        target.status = "production"
        self._write_manifest(target)
        if current:
            try:
                old = self._read_manifest(current)
                old.status = "previous"
                self._write_manifest(old)
            except RegistryError:
                pass
        self._state = {"production": previous, "previous": current}
        self._write_state()
        return target.to_dict()

    def production(self) -> dict[str, Any]:
        release_id = self._state.get("production")
        if not release_id:
            raise RegistryError("no production release")
        return self._read_manifest(release_id).to_dict()

    def list_releases(self) -> list[dict[str, Any]]:
        releases: list[dict[str, Any]] = []
        for path in sorted(self.releases_dir.glob("*/manifest.json")):
            try:
                releases.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
        releases.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return releases
