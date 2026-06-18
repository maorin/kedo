"""
TestStore — 把测试监控信息持久化到本地（<storage_dir>/test_runs.json）

测试 view 原来靠扫任务日志即时算，重启即丢、报告被清就没了。这里把每次回归
（test/report/<date>/ai-regression/）的结构化结果落盘成一份耐久历史：
- 即使套件按天清掉旧报告目录，监控历史仍在
- 重启后照样显示
- 按 (root, date) upsert，一天一条

数据来自 api/routes.py 的 _scan_reports（用例 status 从文件名 pass/fail/blocked 推出）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class TestStore:
    def __init__(self, storage_dir: str = ".kedo/state"):
        self._path = Path(storage_dir) / "test_runs.json"
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
            logger.info(f"TestStore: loaded {len(self._runs)} test run(s) from {self._path}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"TestStore: load failed: {e}")

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
            logger.warning(f"TestStore: persist failed: {e}")

    def sync(self, scanned_runs: list[dict]) -> None:
        """从 _scan_reports 的结果 upsert（按 root::date）。变了才落盘。"""
        changed = False
        for r in scanned_runs or []:
            cases = r.get("cases") or []
            counts = {"pass": 0, "failed": 0, "blocked": 0, "other": 0}
            for c in cases:
                st = c.get("status", "other")
                counts[st] = counts.get(st, 0) + 1
            key = f"{r.get('root')}::{r.get('date')}"
            rec = {
                "key": key,
                "date": r.get("date"),
                "root": r.get("root"),
                "total": len(cases),
                "passed": counts["pass"],
                "failed": counts["failed"],
                "blocked": counts["blocked"],
                "cases": [{"name": c.get("name"), "status": c.get("status")} for c in cases],
                "index_html": r.get("index_html"),
                "index_md": r.get("index_md"),
                "log": r.get("log"),
            }
            if self._runs.get(key) != rec:
                self._runs[key] = rec
                changed = True
        if changed:
            self._persist()

    def list_runs(self) -> list[dict]:
        return sorted(self._runs.values(), key=lambda x: x.get("date", ""), reverse=True)

    def summary(self) -> dict:
        runs = self.list_runs()
        latest = runs[0] if runs else None
        totals = {
            "total": sum(r.get("total", 0) for r in runs),
            "passed": sum(r.get("passed", 0) for r in runs),
            "failed": sum(r.get("failed", 0) for r in runs),
            "blocked": sum(r.get("blocked", 0) for r in runs),
        }
        return {"latest": latest, "totals": totals, "runs": runs}
