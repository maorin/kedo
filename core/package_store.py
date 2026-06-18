"""
PackageStore — 持久化 hci-package-iso skill 的打包结果（<storage_dir>/package_runs.json）

打包产物（crypto ISO + .sha256 + RPM + 日志）生成在远程构建机（如 root@192.168.7.93
的 /root/hci_packager/output/）。这里把每次扫描到的结果落盘成耐久记录，供 dashboard
「打包」view 展示，重启 / 产物被清都不丢。

填充来自 api/routes.py 的 package scan（ssh 到构建机枚举产物）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class PackageStore:
    def __init__(self, storage_dir: str = ".kedo/state"):
        self._path = Path(storage_dir) / "package_runs.json"
        self._runs: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for r in data:
                if isinstance(r, dict) and r.get("key"):
                    self._runs[r["key"]] = r
            logger.info(f"PackageStore: loaded {len(self._runs)} package run(s) from {self._path}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"PackageStore: load failed: {e}")

    def _persist(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self.list_runs(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"PackageStore: persist failed: {e}")

    def record(self, run: dict) -> dict:
        """落盘一条打包记录。key = host::第一个ISO名（无 ISO 时用 host::recorded_at）。"""
        isos = run.get("isos") or []
        first = isos[0]["name"] if isos else (run.get("recorded_at") or "")
        key = f"{run.get('host')}::{first}"
        rec = {"key": key, **run}
        self._runs[key] = rec
        self._persist()
        return rec

    def list_runs(self) -> list[dict]:
        return sorted(self._runs.values(), key=lambda x: x.get("recorded_at", ""), reverse=True)
