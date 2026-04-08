"""
Monitor — 运行时监控

持续监控部署后的服务状态，收集指标，自动告警
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class MetricPoint:
    name: str
    value: float
    timestamp: datetime = field(default_factory=datetime.utcnow)
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class AlertRule:
    name: str
    metric_name: str
    threshold: float
    comparison: str  # "gt", "lt", "eq"
    window_seconds: int = 60
    callback: Optional[Callable] = None


class Monitor:
    """
    运行时监控器

    - 收集系统指标 (CPU、内存、请求延迟等)
    - 收集应用日志
    - 根据告警规则触发通知
    - 提供指标查询接口 (供 Dashboard 展示)
    """

    def __init__(self):
        self._metrics: dict[str, list[MetricPoint]] = {}
        self._alert_rules: list[AlertRule] = []
        self._alerts_fired: list[dict[str, Any]] = []
        self._running = False
        self._callbacks: list[Callable] = []

    def add_metric(self, name: str, value: float, **labels):
        """记录一个指标数据点"""
        point = MetricPoint(name=name, value=value, labels=labels)
        if name not in self._metrics:
            self._metrics[name] = []
        self._metrics[name].append(point)

        # 检查告警
        self._check_alerts(name)

        # 保持数据点数量在合理范围内
        if len(self._metrics[name]) > 10000:
            self._metrics[name] = self._metrics[name][-5000:]

    def add_alert_rule(self, rule: AlertRule):
        """添加告警规则"""
        self._alert_rules.append(rule)
        logger.info(f"Alert rule added: {rule.name}")

    def get_metrics(
        self,
        name: str,
        window_seconds: int = 300,
    ) -> list[MetricPoint]:
        """查询指标数据"""
        if name not in self._metrics:
            return []
        cutoff = datetime.utcnow().timestamp() - window_seconds
        return [
            p for p in self._metrics[name]
            if p.timestamp.timestamp() > cutoff
        ]

    def get_metric_summary(self, name: str, window_seconds: int = 300) -> dict:
        """获取指标摘要统计"""
        points = self.get_metrics(name, window_seconds)
        if not points:
            return {"count": 0}

        values = [p.value for p in points]
        return {
            "count": len(values),
            "min": min(values),
            "max": max(values),
            "avg": sum(values) / len(values),
            "latest": values[-1],
        }

    def on_alert(self, callback: Callable):
        """注册告警回调"""
        self._callbacks.append(callback)

    def get_recent_alerts(self, limit: int = 20) -> list[dict]:
        return self._alerts_fired[-limit:]

    async def start_collection(self, interval: int = 10):
        """启动定时指标收集"""
        self._running = True
        while self._running:
            await self._collect_system_metrics()
            await asyncio.sleep(interval)

    def stop(self):
        self._running = False

    async def _collect_system_metrics(self):
        """收集系统指标"""
        try:
            import psutil
            self.add_metric("cpu_percent", psutil.cpu_percent())
            mem = psutil.virtual_memory()
            self.add_metric("memory_percent", mem.percent)
            self.add_metric("memory_used_mb", mem.used / (1024 * 1024))
        except ImportError:
            pass  # psutil not available

    def _check_alerts(self, metric_name: str):
        """检查告警规则"""
        for rule in self._alert_rules:
            if rule.metric_name != metric_name:
                continue

            summary = self.get_metric_summary(metric_name, rule.window_seconds)
            if summary["count"] == 0:
                continue

            latest = summary["latest"]
            triggered = False

            if rule.comparison == "gt" and latest > rule.threshold:
                triggered = True
            elif rule.comparison == "lt" and latest < rule.threshold:
                triggered = True
            elif rule.comparison == "eq" and latest == rule.threshold:
                triggered = True

            if triggered:
                alert = {
                    "rule": rule.name,
                    "metric": metric_name,
                    "value": latest,
                    "threshold": rule.threshold,
                    "timestamp": datetime.utcnow().isoformat(),
                }
                self._alerts_fired.append(alert)
                logger.warning(f"Alert fired: {rule.name} ({latest} {rule.comparison} {rule.threshold})")

                for cb in self._callbacks:
                    try:
                        cb(alert)
                    except Exception as e:
                        logger.error(f"Alert callback error: {e}")
