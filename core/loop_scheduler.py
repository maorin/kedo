"""
LoopScheduler — /loop 自动循环（M1：模式 A 定时 / 自定步重跑）

把一个任务描述按固定间隔重复创建为 task（interval 模式），或在上一轮跑完后
立即重跑（continuous 模式 = 自定步最简实现）。模式 B（agent 自迭代到目标，带
目标达成判定）留待 M2，本类预留 mode 字段但不实现 iterate。

设计要点：
- 单后台 asyncio task，每 TICK_SECONDS 巡检所有 loop（粒度误差 ≤ TICK 秒）
- 每个 loop 串行：本轮 spawn 的 task 未到终态前不开下一轮，避免任务堆积
- 借 StateManager.get_task_status 判断上一轮是否结束（COMPLETED / FAILED）
- 借 react_agent.start_task 真正创建任务（与 POST /api/tasks 同一执行路径）
- 持久化到 <storage_dir>/loops.json，重启后保留计划（运行态 current_task_id 重置）

详见 docs/roadmap.md 的「/loop 自动循环」段。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

from api.schemas import TaskStatus

logger = logging.getLogger(__name__)

# 终态：本轮 spawn 的 task 到这两个状态就认为一轮结束
_TERMINAL = {TaskStatus.COMPLETED, TaskStatus.FAILED}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


class LoopScheduler:
    TICK_SECONDS = 5

    def __init__(
        self,
        react_agent,
        state_manager,
        broadcast: Optional[Callable[[dict], Awaitable[None]]] = None,
        storage_dir: str = ".kedo/state",
        default_project_path: str = ".",
    ):
        self._agent = react_agent
        self._state = state_manager
        self._broadcast = broadcast  # async fn(payload: dict) | None
        self._loops: dict[str, dict] = {}
        self._path = Path(storage_dir) / "loops.json"
        self._default_project = default_project_path
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._load()

    # ---------------------------------------------------------- persistence
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for lp in data:
                # 重启后无法确定旧 task 是否还活着 → 运行态清零
                lp["current_task_id"] = None
                self._loops[lp["id"]] = lp
            logger.info(f"LoopScheduler: loaded {len(self._loops)} loop(s) from {self._path}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"LoopScheduler: failed to load loops: {e}")

    def _persist(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(list(self._loops.values()), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"LoopScheduler: persist failed: {e}")

    # ---------------------------------------------------------- public API (routes)
    def list_loops(self) -> list[dict]:
        return [self._public(lp) for lp in self._loops.values()]

    def get_loop(self, loop_id: str) -> Optional[dict]:
        lp = self._loops.get(loop_id)
        return self._public(lp) if lp else None

    def create_loop(
        self,
        spec: str,
        mode: str = "interval",
        interval_seconds: Optional[int] = None,
        project_path: Optional[str] = None,
        max_runs: Optional[int] = None,
    ) -> dict:
        """新建一个 loop。mode='interval' 需 interval_seconds；'continuous' 上轮完成即重跑。"""
        if mode == "interval" and not interval_seconds:
            mode = "continuous"  # 没给间隔就退化为自定步
        loop_id = "lp-" + uuid.uuid4().hex[:6]
        now = _now()
        lp = {
            "id": loop_id,
            "spec": spec,
            "mode": mode,  # "interval" | "continuous"  (M2: "iterate")
            "interval_seconds": interval_seconds,
            "project_path": project_path or self._default_project,
            "max_runs": max_runs,
            "status": "running",
            "run_count": 0,
            "created_at": _iso(now),
            "next_run_at": _iso(now),  # 第一轮立即触发
            "last_run_at": None,
            "current_task_id": None,
            "history": [],
        }
        self._loops[loop_id] = lp
        self._persist()
        logger.info(f"LoopScheduler: created {loop_id} mode={mode} interval={interval_seconds} spec={spec[:60]!r}")
        return self._public(lp)

    def toggle(self, loop_id: str) -> Optional[dict]:
        lp = self._loops.get(loop_id)
        if not lp:
            return None
        if lp["status"] == "running":
            lp["status"] = "paused"
        else:
            lp["status"] = "running"
            # resume 后让 interval loop 尽快补跑（不积压历史窗口）
            if lp["mode"] == "interval":
                lp["next_run_at"] = _iso(_now())
        self._persist()
        return self._public(lp)

    def delete(self, loop_id: str) -> bool:
        existed = self._loops.pop(loop_id, None) is not None
        if existed:
            self._persist()
        return existed

    def history(self, loop_id: str) -> Optional[list]:
        lp = self._loops.get(loop_id)
        return list(lp.get("history", [])) if lp else None

    # ---------------------------------------------------------- background runner
    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())
            logger.info("LoopScheduler started")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.TICK_SECONDS)
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.error(f"LoopScheduler tick error: {e}")

    async def _tick(self) -> None:
        async with self._lock:
            changed = False
            for lp in list(self._loops.values()):
                if lp["status"] != "running":
                    continue

                # 1) 本轮 spawn 的 task 还没到终态 → 等
                cur = lp.get("current_task_id")
                if cur:
                    st = self._task_status(cur)
                    if st not in _TERMINAL:
                        continue
                    self._finish_run(lp, cur, st)
                    lp["current_task_id"] = None
                    changed = True

                # 2) max_runs 到顶 → 标完成
                if lp.get("max_runs") and lp["run_count"] >= lp["max_runs"]:
                    if lp["status"] != "completed":
                        lp["status"] = "completed"
                        changed = True
                        await self._emit("loop_completed", lp)
                    continue

                # 3) 该开下一轮就开
                if self._due(lp):
                    await self._spawn(lp)
                    changed = True

            if changed:
                self._persist()

    def _due(self, lp: dict) -> bool:
        if lp["mode"] == "continuous":
            return True  # 到这里说明上一轮已结束（current_task_id 已清）
        nra = lp.get("next_run_at")
        if not nra:
            return True
        try:
            return _now() >= datetime.fromisoformat(nra)
        except Exception:  # noqa: BLE001
            return True

    async def _spawn(self, lp: dict) -> None:
        task_id = uuid.uuid4().hex[:8]
        description = lp["spec"]
        run_no = lp["run_count"] + 1
        try:
            await self._agent.start_task(
                task_id=task_id,
                description=description,
                project_path=lp["project_path"],
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"LoopScheduler: start_task failed for {lp['id']}: {e}")
            lp["history"].append({
                "run": run_no,
                "task_id": task_id,
                "started_at": _iso(_now()),
                "finished_at": _iso(_now()),
                "status": "spawn_error",
                "error": str(e),
            })
            # interval 模式仍排下一轮，避免一次失败就卡死
            if lp["mode"] == "interval" and lp.get("interval_seconds"):
                lp["next_run_at"] = _iso(_now() + timedelta(seconds=lp["interval_seconds"]))
            return

        lp["run_count"] = run_no
        lp["current_task_id"] = task_id
        lp["last_run_at"] = _iso(_now())
        lp["history"].append({
            "run": run_no,
            "task_id": task_id,
            "started_at": _iso(_now()),
            "finished_at": None,
            "status": "running",
        })
        if lp["mode"] == "interval" and lp.get("interval_seconds"):
            lp["next_run_at"] = _iso(_now() + timedelta(seconds=lp["interval_seconds"]))
        await self._emit("loop_execution", lp, extra={"task_id": task_id, "run": run_no})

    def _finish_run(self, lp: dict, task_id: str, status) -> None:
        sv = status.value if hasattr(status, "value") else str(status)
        for h in reversed(lp["history"]):
            if h.get("task_id") == task_id and h.get("finished_at") is None:
                h["finished_at"] = _iso(_now())
                h["status"] = sv
                break

    def _task_status(self, task_id: str):
        try:
            resp = self._state.get_task_status(task_id)
            return resp.status if resp else TaskStatus.FAILED
        except Exception:  # noqa: BLE001
            return TaskStatus.FAILED

    async def _emit(self, kind: str, lp: dict, extra: Optional[dict] = None) -> None:
        if not self._broadcast:
            return
        try:
            data = self._public(lp)
            if extra:
                data = {**data, **extra}
            await self._broadcast({"type": "loop_event", "event": kind, "data": data})
        except Exception as e:  # noqa: BLE001
            logger.debug(f"LoopScheduler emit failed: {e}")

    @staticmethod
    def _public(lp: Optional[dict]) -> Optional[dict]:
        """对外视图：history 只保留最近 20 条，避免列表接口体积膨胀。"""
        if not lp:
            return lp
        d = dict(lp)
        d["history"] = lp.get("history", [])[-20:]
        return d
