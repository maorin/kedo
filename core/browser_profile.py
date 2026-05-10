"""
Isolated browser profile — Agent 自己开 Chrome 查资料专用（M4）。

设计参考：docs/deep-dives/browser-bridge-design.md §4.2 + §7.3。

为什么需要：
- M2/M3 让 ReactAgent 操作"用户主浏览器"（场景 A）—— 受密码硬规则、信任窗口
  限制，不能让 agent 随便点登录、不污染用户登录态
- 但 agent 自己也常需要浏览（如 build 失败查 stackoverflow）。如果用主浏览器
  会污染 cookies，且每次都要权限弹窗，体验差
- M4 解法：kedo 起一个**独立 chrome 实例**，独立 user-data-dir，加载同一个
  kedo-browser-bridge 插件。插件读 EXTENSION_DIR 下的 kedo-config.json
  报 role=agent，server 据此路由 prefer_role="agent" 的工具调用

关键不变量：
- kedo 主进程退出 → 隔离 chrome 也关
- 30 min idle → 自动关（节省内存）
- 同时只一个 agent profile（不支持并行 research）
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


PROFILE_DIR = Path.home() / ".kedo" / "browser-profile"
EXTENSION_PACK_DIR = Path.home() / ".kedo" / "browser-extension-pack"
AGENT_TOKEN_PATH = Path.home() / ".config" / "kedo" / "browser_token_agent"
DEFAULT_IDLE_MINUTES = 30
DEFAULT_BUILT_DIST = Path.home() / "project" / "kedo-browser-bridge" / "dist"

CHROME_BINARIES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)


def get_or_create_agent_token() -> str:
    """Return the long-lived agent-profile token; create + persist if missing.
    Distinct from the user browser_token so server can decide role by which
    token a hello frame presented."""
    if AGENT_TOKEN_PATH.exists():
        token = AGENT_TOKEN_PATH.read_text(encoding="utf-8").strip()
        if token:
            return token
    AGENT_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    AGENT_TOKEN_PATH.write_text(token, encoding="utf-8")
    try:
        AGENT_TOKEN_PATH.chmod(0o600)
    except OSError:
        pass
    logger.info(f"browser_profile: created agent token at {AGENT_TOKEN_PATH}")
    return token


def _find_chrome_binary() -> Optional[str]:
    for cmd in CHROME_BINARIES:
        if "/" in cmd and Path(cmd).exists():
            return cmd
        located = shutil.which(cmd)
        if located:
            return located
    return None


class IsolatedBrowserProfile:
    """Manages a single kedo-launched headed Chrome instance with role=agent."""

    def __init__(
        self,
        ws_url: str,
        bridge,
        idle_minutes: int = DEFAULT_IDLE_MINUTES,
        extension_source_dir: Optional[Path] = None,
    ):
        self._ws_url = ws_url
        self._bridge = bridge
        self._idle_seconds = idle_minutes * 60
        self._extension_source_dir = Path(
            extension_source_dir or os.environ.get("KEDO_AGENT_EXT_PATH") or DEFAULT_BUILT_DIST
        )
        self._proc: Optional[subprocess.Popen] = None
        self._start_lock = asyncio.Lock()
        self._idle_task: Optional[asyncio.Task] = None
        self._last_activity: float = time.monotonic()

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def mark_activity(self) -> None:
        self._last_activity = time.monotonic()

    async def ensure_running(self, timeout: float = 30.0) -> None:
        """Start the isolated chrome if not alive; wait for plugin to connect with role=agent.
        Raises RuntimeError if chrome binary missing, extension pack missing, or session
        does not connect within `timeout`."""
        async with self._start_lock:
            if self.is_running and self._has_agent_session():
                self.mark_activity()
                return

            if not self.is_running:
                self._spawn_chrome()

            # Wait for the plugin in the spawned chrome to connect + handshake as agent
            start = time.monotonic()
            while time.monotonic() - start < timeout:
                if self._has_agent_session():
                    self.mark_activity()
                    self._ensure_idle_watcher()
                    logger.info("browser_profile: agent session connected")
                    return
                if self._proc is not None and self._proc.poll() is not None:
                    raise RuntimeError(
                        f"chrome exited prematurely (rc={self._proc.returncode}) before "
                        f"agent session connected; check ~/.kedo/browser-profile permissions "
                        f"and that the extension dist exists at {self._extension_source_dir}"
                    )
                await asyncio.sleep(0.5)
            raise RuntimeError(
                f"agent session did not connect within {timeout}s; check kedo log + "
                f"{self._extension_source_dir} contents"
            )

    def _has_agent_session(self) -> bool:
        for s in self._bridge._sessions.values():
            if s.role == "agent":
                return True
        return False

    def _spawn_chrome(self) -> None:
        chrome = _find_chrome_binary()
        if chrome is None:
            raise RuntimeError(
                "google-chrome binary not found; install chrome or set PATH"
            )

        if not self._extension_source_dir.exists():
            raise RuntimeError(
                f"extension dist not found at {self._extension_source_dir}; "
                f"run `pnpm build` in kedo-browser-bridge or set KEDO_AGENT_EXT_PATH"
            )

        # Stage the extension pack: copy source dist → ~/.kedo/browser-extension-pack/
        # then write kedo-config.json into it. Chrome will load this writable copy.
        EXTENSION_PACK_DIR.parent.mkdir(parents=True, exist_ok=True)
        if EXTENSION_PACK_DIR.exists():
            shutil.rmtree(EXTENSION_PACK_DIR)
        shutil.copytree(self._extension_source_dir, EXTENSION_PACK_DIR)

        token = get_or_create_agent_token()
        config_path = EXTENSION_PACK_DIR / "kedo-config.json"
        config_path.write_text(
            json.dumps({
                "role": "agent",
                "token": token,
                "ws_url": self._ws_url,
            }, indent=2),
            encoding="utf-8",
        )
        # Manifest must declare kedo-config.json in web_accessible_resources so that
        # the service worker (running with chrome-extension://<id>/ origin) can fetch it.
        self._patch_manifest_for_agent_config()

        # Kill any orphan chrome processes still holding our user-data-dir
        # (typical after a previous kedo crash or kill -9 not propagating to chrome)
        # —— if a stale chrome locks PROFILE_DIR, our new chrome silently exits
        # because the profile is already in use.
        self._kill_orphan_chrome()

        # Fresh user-data-dir each time → forces chrome.runtime.onInstalled to fire,
        # which is one of two SW-wake paths (the other is content_script.cs_loaded).
        # Cookies/cache lost between sessions is fine for research use case.
        if PROFILE_DIR.exists():
            shutil.rmtree(PROFILE_DIR)
        PROFILE_DIR.parent.mkdir(parents=True, exist_ok=True)

        # Headless by default — kedo typically runs as a daemon (no $DISPLAY); a
        # headed window would fail to open and chrome would exit rc=1. Set
        # KEDO_AGENT_BROWSER_HEADED=1 to opt into headed mode for debugging.
        # `--headless=new` is the modern headless mode (Chrome 109+) which fully
        # supports --load-extension; the legacy --headless does not.
        headed = os.environ.get("KEDO_AGENT_BROWSER_HEADED", "").lower() in ("1", "true", "yes")
        mode_flags = (
            []
            if headed
            else ["--headless=new", "--disable-gpu", "--no-sandbox"]
        )
        # Chrome 137+ added a policy that silently ignores `--load-extension` in
        # Google Chrome (not Chromium) outside of dev mode. Without this flag we
        # see in chrome's stderr:
        #     "--load-extension is not allowed in Google Chrome, ignoring."
        # and the kedo-browser-bridge plugin never loads → no agent ws session.
        mode_flags.append("--disable-features=DisableLoadExtensionCommandLineSwitch")
        # Open a kedo-served HTTP page on launch, NOT about:blank —— headless
        # chrome's MV3 service worker won't auto-start without an event. Loading a
        # real URL triggers content_script injection (matches <all_urls>); the CS
        # then sends a wake-up message to the SW, which calls ensureClient() and
        # connects via ws.
        bootstrap_url = self._bootstrap_url()

        cmd = [
            chrome,
            *mode_flags,
            f"--user-data-dir={PROFILE_DIR}",
            f"--load-extension={EXTENSION_PACK_DIR}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            bootstrap_url,
        ]
        logger.info(
            f"browser_profile: launching chrome (headless={not headed}): "
            f"{chrome} … --user-data-dir={PROFILE_DIR}"
        )

        # Capture chrome stderr to ~/.kedo/chrome-agent.log so we can diagnose
        # why agent SW isn't connecting (headless chrome MV3 SW startup has
        # known quirks; this log is invaluable for "why didn't SW wake up").
        chrome_log = Path.home() / ".kedo" / "chrome-agent.log"
        chrome_log.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(chrome_log, "w", encoding="utf-8")  # noqa: SIM115
        # detach into its own process group so a kedo SIGINT doesn't kill chrome
        # (we still cleanup on kedo shutdown via atexit)
        self._proc = subprocess.Popen(
            cmd + ["--enable-logging=stderr", "--v=0"],
            stdout=log_fh,
            stderr=log_fh,
            stdin=subprocess.DEVNULL,
            preexec_fn=os.setsid if os.name != "nt" else None,
        )
        logger.info(f"browser_profile: chrome stderr → {chrome_log}")

    def _kill_orphan_chrome(self) -> None:
        """Find chrome processes using our PROFILE_DIR and kill them.

        Used before spawning a new isolated chrome — chrome locks the
        user-data-dir and a stale process here causes our new launch to
        silently exit.
        """
        try:
            # pgrep with literal -f matching of the directory path; no regex meta chars
            result = subprocess.run(
                ["pgrep", "-f", f"--user-data-dir={PROFILE_DIR}"],
                capture_output=True, text=True, timeout=3,
            )
            pids = [
                int(line.strip())
                for line in result.stdout.splitlines()
                if line.strip().isdigit()
            ]
        except Exception as exc:
            logger.debug(f"browser_profile: pgrep orphan check failed: {exc}")
            return
        if not pids:
            return
        logger.info(f"browser_profile: killing {len(pids)} orphan chrome process(es): {pids}")
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception as exc:
                logger.warning(f"browser_profile: kill {pid} failed: {exc}")
        time.sleep(1.0)  # give chrome a moment to release file locks

    def _bootstrap_url(self) -> str:
        """Convert ws://host:port/api/ws/browser → http://host:port/api/browser-bridge/agent-bootstrap"""
        base = self._ws_url.replace("ws://", "http://").replace("wss://", "https://")
        host_part = base.split("/api/", 1)[0]  # http://host:port
        return f"{host_part}/api/browser-bridge/agent-bootstrap"

    def _patch_manifest_for_agent_config(self) -> None:
        """Append kedo-config.json to web_accessible_resources so SW can fetch it."""
        manifest_path = EXTENSION_PACK_DIR / "manifest.json"
        if not manifest_path.exists():
            return
        try:
            mf = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"browser_profile: manifest parse failed: {exc}")
            return
        war = mf.get("web_accessible_resources") or []
        # Ensure at least one entry exists for our config; idempotent
        target = "kedo-config.json"
        already = any(target in (e.get("resources") or []) for e in war)
        if not already:
            war.append({
                "matches": ["<all_urls>"],
                "resources": [target],
                "use_dynamic_url": False,
            })
            mf["web_accessible_resources"] = war
            manifest_path.write_text(
                json.dumps(mf, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    def _ensure_idle_watcher(self) -> None:
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.create_task(self._idle_watch_loop())

    async def _idle_watch_loop(self) -> None:
        try:
            while self.is_running:
                await asyncio.sleep(60)
                if time.monotonic() - self._last_activity > self._idle_seconds:
                    logger.info(
                        f"browser_profile: idle > {self._idle_seconds}s, closing chrome"
                    )
                    await self.stop()
                    return
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning(f"browser_profile: idle watcher errored: {exc}")

    async def stop(self) -> None:
        if not self.is_running:
            return
        proc = self._proc
        try:
            if os.name != "nt":
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                else:
                    proc.kill()
        except Exception as exc:
            logger.warning(f"browser_profile: stop errored: {exc}")
        finally:
            self._proc = None
            logger.info("browser_profile: chrome stopped")

    def status(self) -> dict:
        return {
            "running": self.is_running,
            "extension_dir": str(self._extension_source_dir),
            "profile_dir": str(PROFILE_DIR),
            "idle_seconds_remaining": (
                round(self._idle_seconds - (time.monotonic() - self._last_activity), 1)
                if self.is_running else None
            ),
        }
