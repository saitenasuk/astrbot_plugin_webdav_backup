import asyncio
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from uuid import uuid4
from zipfile import ZipFile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from yarl import URL

from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import error_response, json_response, request
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .archive import NAME, create_archive
from .webdav import WebDAVError, collection_url, upload, verify_remote

SCHEMA = json.loads(Path(__file__).with_name("_conf_schema.json").read_text("utf-8"))
MODES = ("local", "webdav")


class BackupDashboard(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        # Keep each destination's temp selection from versions with item lists.
        migrated = {}
        for key in ("local_items", "webdav_items"):
            if isinstance(config.get(key), list):
                migrated[key] = "temp" in config[key]
        if migrated:
            config.save_config(migrated)
        self.config = config
        self.scheduler = None
        self.tasks = {}
        self.phases = dict.fromkeys(MODES, "空闲")
        self.lock = asyncio.Lock()
        self.recheck_lock = asyncio.Lock()
        self.closing = False
        self.history = []
        self.plan_errors = {}
        self.history_file = StarTools.get_data_dir(NAME) / "runs.json"
        for endpoint, method, handler in [
            ("dashboard", "GET", self.dashboard),
            ("settings", "POST", self.settings),
            ("test-connection", "POST", self.test_connection),
            ("verify-remote", "POST", self.verify_remote),
            ("run", "POST", self.run_now),
        ]:
            context.register_web_api(
                f"/{NAME}/{endpoint}", handler, [method], f"Backup {endpoint}"
            )

    async def initialize(self):
        if self.history_file.is_file():
            try:
                records = json.loads(self.history_file.read_text("utf-8"))
                if isinstance(records, list):
                    self.history = records[:30]
            except (OSError, ValueError):
                logger.warning("Could not load backup history")
        self.scheduler = AsyncIOScheduler()
        self.scheduler.start()
        self._apply_plans()

    def _apply_plans(self):
        self.scheduler.remove_all_jobs()
        self.plan_errors.clear()
        for mode in MODES:
            if not self.config.get(f"{mode}_enabled"):
                continue
            try:
                trigger = CronTrigger.from_crontab(
                    self.config[f"{mode}_cron"],
                    timezone=ZoneInfo(self.config[f"{mode}_timezone"]),
                )
                self.scheduler.add_job(
                    self._scheduled,
                    trigger,
                    args=[mode],
                    id=mode,
                    coalesce=True,
                    misfire_grace_time=300,
                )
            except (ValueError, ZoneInfoNotFoundError) as exc:
                self.plan_errors[mode] = "计划无效，请检查 Cron 和时区。"
                logger.warning("Invalid schedule for %s: %s", mode, type(exc).__name__)

    async def dashboard(self):
        public = {
            key: self.config.get(key, spec["default"])
            for key, spec in SCHEMA.items()
            if key != "password"
        }
        jobs = {
            job.id: job.next_run_time.isoformat()
            for job in self.scheduler.get_jobs()
            if job.next_run_time
        }
        return json_response(
            {
                "settings": public,
                "password_set": bool(
                    self.config.get("password") or os.getenv("ASTRBOT_WEBDAV_PASSWORD")
                ),
                "phases": self.phases,
                "next": jobs,
                "plan_errors": self.plan_errors,
                "history": self.history,
            }
        )

    async def settings(self):
        payload = await request.json(default=None)
        if (
            not isinstance(payload, dict)
            or not payload
            or payload.keys() - SCHEMA.keys()
        ):
            return error_response("设置字段无效", status_code=400)
        candidate = {
            key: self.config.get(key, spec["default"]) for key, spec in SCHEMA.items()
        }
        try:
            for key, value in payload.items():
                spec = SCHEMA[key]
                if type(value) is not {"bool": bool, "string": str}[spec["type"]]:
                    raise ValueError(f"无效设置项 {key}")
                if isinstance(value, str) and len(value) > 4096:
                    raise ValueError(f"设置值过长 {key}")
                candidate[key] = value
            for mode in MODES:
                try:
                    CronTrigger.from_crontab(
                        candidate[f"{mode}_cron"],
                        timezone=ZoneInfo(candidate[f"{mode}_timezone"]),
                    )
                except (ValueError, ZoneInfoNotFoundError) as exc:
                    raise ValueError("请检查五段 Cron 表达式与 IANA 时区") from exc
            if candidate["url"] or candidate["webdav_enabled"]:
                collection_url(
                    candidate["url"], candidate["allow_http"], candidate["remote_dir"]
                )
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        self.config.save_config(candidate)
        self._apply_plans()
        return json_response({"saved": True})

    async def test_connection(self):
        """Exercise a transfer using the current dashboard inputs without saving them.

        Returns:
            A JSON response confirming cleanup or describing the failed operation.
        """
        payload = await request.json(default=None)
        keys = {"url", "remote_dir", "username", "password", "allow_http"}
        if (
            not isinstance(payload, dict)
            or "url" not in payload
            or payload.keys() - keys
        ):
            return error_response("连接设置字段无效", status_code=400)
        for key, value in payload.items():
            if key == "allow_http":
                if type(value) is not bool:
                    return error_response("HTTP 开关无效", status_code=400)
            elif not isinstance(value, str) or len(value) > 4096:
                return error_response("连接设置值无效", status_code=400)
        try:
            collection = collection_url(
                payload["url"],
                payload.get("allow_http", False),
                payload.get("remote_dir", "/"),
            )
            username = payload.get("username", "")
            env_password = os.getenv("ASTRBOT_WEBDAV_PASSWORD")
            password = (
                env_password
                or payload.get("password")
                or self.config.get("password", "")
            )
            if password and username and (env_password or not payload.get("password")):
                # A draft endpoint must not receive credentials for a different server.
                saved = self.config.get("url", "")
                if (
                    not saved
                    or URL(collection_url(saved, True)).origin()
                    != URL(collection).origin()
                    or self.config.get("username", "") != username
                ):
                    return error_response(
                        "地址或用户名已更改。请重新输入密码，使用环境变量密码时请先保存连接设置。",
                        status_code=400,
                    )
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        try:
            with tempfile.TemporaryDirectory() as work:
                path = Path(work) / f"astrbot_connection_test_{uuid4().hex}.zip"
                with ZipFile(path, "w") as archive:
                    archive.writestr("test.txt", "AstrBot WebDAV connection test")
                await upload(path, collection, username, password, probe=True)
            logger.info("WebDAV connection test completed for %s", collection)
            return json_response({"verified": True})
        except (WebDAVError, OSError) as exc:
            reason = str(exc) if isinstance(exc, WebDAVError) else type(exc).__name__
            logger.warning(
                "WebDAV connection test failed for %s: %s", collection, reason
            )
            return error_response(reason, status_code=502)

    async def verify_remote(self):
        """Recheck one recorded archive using the saved collection and credentials.

        Returns:
            A timestamped check result, or an error if no check was performed.
        """
        payload = await request.json(default=None)
        if (
            not isinstance(payload, dict)
            or payload.keys() != {"time"}
            or not isinstance(payload["time"], str)
        ):
            return error_response("请选择一条 WebDAV 备份记录", status_code=400)
        record = next(
            (
                r
                for r in self.history
                if r.get("time") == payload["time"] and r.get("mode") == "webdav"
            ),
            None,
        )
        if not record:
            return error_response("该 WebDAV 记录已不在运行历史中", status_code=404)
        name, size = record.get("name"), record.get("size")
        if not (
            record.get("ok")
            and record.get("remote")
            and isinstance(name, str)
            and re.fullmatch(r"[A-Za-z0-9_.-]+\.zip", name)
            and type(size) is int
            and size >= 0
        ):
            return error_response("该记录没有可复查的远端 ZIP 回执", status_code=400)
        try:
            collection = collection_url(
                self.config.get("url", ""),
                self.config.get("allow_http", False),
                self.config.get("remote_dir", "/"),
            )
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        # Only the saved collection determines where credentials may be sent.
        target = collection + name
        if target != record["remote"]:
            return error_response(
                "当前 WebDAV 目标与历史目录不同，未发送复查请求", status_code=409
            )
        if self.recheck_lock.locked():
            return error_response("已有远端复查正在进行，请稍后重试", status_code=409)
        async with self.recheck_lock:
            try:
                await verify_remote(
                    target,
                    self.config.get("username", ""),
                    os.getenv("ASTRBOT_WEBDAV_PASSWORD")
                    or self.config.get("password", ""),
                    size,
                )
                check = {"ok": True}
            except WebDAVError as exc:
                check = {"ok": False, "error": str(exc)}
            check["time"] = datetime.now(timezone.utc).isoformat()
            record["recheck"] = check
            try:
                temp = self.history_file.with_suffix(".tmp")
                temp.write_text(json.dumps(self.history, ensure_ascii=False), "utf-8")
                temp.replace(self.history_file)
            except OSError:
                logger.warning("Could not persist WebDAV recheck history")
            return json_response(check)

    async def _scheduled(self, mode):
        self._enqueue(mode)

    def _enqueue(self, mode):
        if self.closing or (mode in self.tasks and not self.tasks[mode].done()):
            return False
        self.phases[mode] = "排队中"
        self.tasks[mode] = asyncio.create_task(self._backup(mode))
        return True

    async def run_now(self):
        payload = await request.json(default={})
        mode = payload.get("mode") if isinstance(payload, dict) else None
        if mode not in MODES:
            return error_response("请选择本地或 WebDAV 备份", status_code=400)
        if mode == "webdav":
            try:
                collection_url(
                    self.config.get("url", ""),
                    self.config.get("allow_http", False),
                    self.config.get("remote_dir", "/"),
                )
            except ValueError as exc:
                return error_response(str(exc), status_code=400)
        if not self._enqueue(mode):
            return error_response("任务已排队或正在运行", status_code=409)
        return json_response({"queued": True})

    async def _backup(self, mode):
        async with self.lock:
            now = datetime.now(timezone.utc)
            started = monotonic()
            stage_started = started
            stage = "准备"
            self.phases[mode] = "正在准备"
            record = {"mode": mode, "time": now.isoformat(), "ok": False, "stages": {}}

            def set_stage(name: str | None):
                """Finish timing the current stage and update the live dashboard.

                Args:
                    name: Next stage, or None when the run has finished.
                """
                nonlocal stage, stage_started
                tick = monotonic()
                record["stages"][stage] = round(tick - stage_started, 3)
                stage_started = tick
                if name is not None:
                    stage = name
                    self.phases[mode] = f"正在{name}"
                else:
                    self.phases[mode] = "空闲"

            try:
                settings = dict(self.config)
                data = Path(get_astrbot_data_path())
                backups = data / "backups"
                backups.mkdir(parents=True, exist_ok=True)
                if mode == "webdav":
                    collection = collection_url(
                        settings.get("url", ""),
                        settings.get("allow_http", False),
                        settings.get("remote_dir", "/"),
                    )
                name = (
                    f"astrbot_backup_{now:%Y%m%d_%H%M%S}_{mode}_{uuid4().hex[:12]}.zip"
                )
                if mode == "webdav":
                    record["remote"] = collection + name
                logger.info(
                    "Starting %s backup %s (include_temp=%s, destination=%s)",
                    mode,
                    name,
                    settings.get(f"{mode}_items", False),
                    record.get("remote", backups),
                )
                with tempfile.TemporaryDirectory(
                    prefix=".backup-", dir=backups
                ) as work:
                    path = Path(work) / name
                    set_stage("打包")
                    await create_archive(
                        data,
                        path,
                        self.context.get_db(),
                        self.context.kb_manager,
                        include_temp=settings.get(f"{mode}_items", False),
                        plugin_folder=Path(__file__).parent.name,
                    )
                    size = path.stat().st_size
                    logger.info("Created %s archive %s (%d bytes)", mode, name, size)
                    if mode == "local":
                        set_stage("保存")
                        path.replace(backups / name)
                    else:
                        await upload(
                            path,
                            collection,
                            settings.get("username", ""),
                            os.getenv("ASTRBOT_WEBDAV_PASSWORD")
                            or settings.get("password", ""),
                            on_phase=set_stage,
                        )
                record.update(ok=True, name=name, size=size)
                logger.info("%s backup completed: %s (%d bytes)", mode, name, size)
            except Exception as exc:
                reason = (
                    str(exc)
                    if str(exc).startswith(("WebDAV ", "请填写", "知识库管理器"))
                    else type(exc).__name__
                )
                record["failed_phase"] = stage
                record["error"] = reason
                logger.error(
                    "%s backup failed (destination=%s): %s",
                    mode,
                    record.get("remote", mode),
                    reason,
                )
            finally:
                set_stage(None)
                record["duration"] = round(monotonic() - started, 3)
                self.history = [record, *self.history][:30]
                self._save_history()

    async def terminate(self):
        self.closing = True
        if self.scheduler:
            self.scheduler.shutdown(wait=False)
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
