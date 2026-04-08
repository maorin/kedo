"""
Deployer — 自动部署执行器

支持多环境部署、回滚机制、健康检查
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from tools.shell_executor import ShellExecutorTool

logger = logging.getLogger(__name__)


class DeployEnv(str, Enum):
    DEV = "dev"
    STAGING = "staging"
    PRODUCTION = "production"


@dataclass
class DeployResult:
    success: bool
    environment: str
    version: str = ""
    url: str = ""
    error: Optional[str] = None
    rollback_version: Optional[str] = None


@dataclass
class DeployConfig:
    """部署配置"""
    environment: DeployEnv = DeployEnv.DEV
    deploy_command: str = ""
    health_check_url: str = ""
    health_check_timeout: int = 60
    rollback_command: str = ""
    pre_deploy_hooks: list[str] = field(default_factory=list)
    post_deploy_hooks: list[str] = field(default_factory=list)


class Deployer:
    """自动部署器"""

    def __init__(self, shell: ShellExecutorTool):
        self._shell = shell
        self._deploy_history: list[DeployResult] = []

    async def deploy(
        self,
        project_path: str,
        config: DeployConfig,
    ) -> DeployResult:
        """执行部署"""
        logger.info(f"Deploying to {config.environment.value}...")

        # 1. Pre-deploy hooks
        for hook in config.pre_deploy_hooks:
            result = await self._shell.execute(command=hook, working_dir=project_path)
            if not result.success:
                return DeployResult(
                    success=False,
                    environment=config.environment.value,
                    error=f"Pre-deploy hook failed: {result.error}",
                )

        # 2. 获取当前版本 (用于回滚)
        version_result = await self._shell.execute(
            command="git rev-parse --short HEAD",
            working_dir=project_path,
        )
        current_version = version_result.output.strip() if version_result.success else "unknown"

        # 3. 执行部署
        if config.deploy_command:
            deploy_result = await self._shell.execute(
                command=config.deploy_command,
                working_dir=project_path,
                timeout=300,
            )
            if not deploy_result.success:
                return DeployResult(
                    success=False,
                    environment=config.environment.value,
                    version=current_version,
                    error=f"Deploy failed: {deploy_result.error}",
                    rollback_version=self._get_last_successful_version(config.environment),
                )

        # 4. 健康检查
        if config.health_check_url:
            healthy = await self._health_check(config.health_check_url, config.health_check_timeout)
            if not healthy:
                # 自动回滚
                if config.rollback_command:
                    await self._shell.execute(command=config.rollback_command, working_dir=project_path)
                return DeployResult(
                    success=False,
                    environment=config.environment.value,
                    version=current_version,
                    error="Health check failed, rolled back",
                )

        # 5. Post-deploy hooks
        for hook in config.post_deploy_hooks:
            await self._shell.execute(command=hook, working_dir=project_path)

        result = DeployResult(
            success=True,
            environment=config.environment.value,
            version=current_version,
        )
        self._deploy_history.append(result)

        logger.info(f"Deploy successful: {config.environment.value} @ {current_version}")
        return result

    async def rollback(self, project_path: str, config: DeployConfig, target_version: str) -> DeployResult:
        """回滚到指定版本"""
        logger.warning(f"Rolling back to {target_version}...")
        if config.rollback_command:
            result = await self._shell.execute(
                command=config.rollback_command.replace("{version}", target_version),
                working_dir=project_path,
            )
            return DeployResult(
                success=result.success,
                environment=config.environment.value,
                version=target_version,
                error=result.error if not result.success else None,
            )
        return DeployResult(success=False, environment=config.environment.value, error="No rollback command configured")

    async def _health_check(self, url: str, timeout: int) -> bool:
        """健康检查"""
        import asyncio
        for _ in range(timeout // 5):
            result = await self._shell.execute(
                command=f"curl -sf -o /dev/null -w '%{{http_code}}' {url}",
                timeout=10,
            )
            if result.success and result.output.strip() == "200":
                return True
            await asyncio.sleep(5)
        return False

    def _get_last_successful_version(self, env: DeployEnv) -> Optional[str]:
        for result in reversed(self._deploy_history):
            if result.success and result.environment == env.value:
                return result.version
        return None
