"""
DeployStore — 持久化 hci-iso-deploy skill 的 ISO 部署结果（<storage_dir>/deploy_runs.json）

ISO 部署在目标机上完成（传 ISO → 校验 → loop 挂载 → install.sh work → 验证服务）。
这里把每次扫描到的目标机部署状态落盘成耐久记录，供 dashboard「部署」view 展示。

数据来自 api/routes.py 的 deploy iso scan（ssh 到目标机枚举 ISO/挂载/install 日志/服务）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class DeployStore:
    def __init__(self, storage_dir: str = ".kedo/state"):
        self._path = Path(storage_dir) / "deploy_runs.json"
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
            logger.info(f"DeployStore: loaded {len(self._runs)} deploy run(s) from {self._path}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"DeployStore: load failed: {e}")

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
            logger.warning(f"DeployStore: persist failed: {e}")

    def record(self, run: dict) -> dict:
        """落盘一条部署记录。key = target::iso（无 iso 时 target::recorded_at）。"""
        iso = (run.get("iso") or {}).get("name") if isinstance(run.get("iso"), dict) else None
        key = f"{run.get('target')}::{iso or run.get('recorded_at') or ''}"
        rec = {"key": key, **run}
        self._runs[key] = rec
        self._persist()
        return rec

    def list_runs(self) -> list[dict]:
        return sorted(self._runs.values(), key=lambda x: x.get("recorded_at", ""), reverse=True)
