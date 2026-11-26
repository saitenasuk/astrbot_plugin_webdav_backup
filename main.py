import asyncio
import json
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
from .webdav import WebDAVError, collection_url, prune_remote, upload

SCHEMA = json.loads(Path(__file__).with_name("_conf_schema.json").read_text("utf-8"))
MODES = ("local", "webdav")
MAX_KEEP_COUNT = 1000
BACKUP_NAME_PATTERNS = {
    mode: re.compile(rf"^astrbot_backup_\d{{8}}_\d{{6}}_{mode}_[0-9a-f]{{12}}\.zip$")
    for mode in MODES
}


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
        self.phase_started: dict[str, str | None] = dict.fromkeys(MODES, None)
        self.lock = asyncio.Lock()
        self.closing = False
        self.history = []
        self.plan_errors = {}
        self.history_file = StarTools.get_data_dir(NAME) / "runs.json"
        for endpoint, method, handler in [
            ("dashboard", "GET", self.dashboard),
            ("settings", "POST", self.settings),
            ("cron-preview", "POST", self.cron_preview),
            ("test-connection", "POST", self.test_connection),
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
        """Return full dashboard telemetry, configuration summary, and run history.

        Returns:
            A JSON response containing settings, phases, schedules, storage stats,
            and recent run history.
        """
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
        backups_dir = Path(get_astrbot_data_path()) / "backups"
        total_count = total_size = plugin_count = plugin_size = 0
        if backups_dir.is_dir():
            try:
                for entry in backups_dir.iterdir():
                    if (
                        entry.is_symlink()
                        or not entry.is_file()
                        or not entry.name.endswith(".zip")
                    ):
                        continue
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        continue
                    total_count += 1
                    total_size += size
                    if BACKUP_NAME_PATTERNS["local"].fullmatch(entry.name):
                        plugin_count += 1
                        plugin_size += size
            except OSError:
                logger.warning("Could not inspect local backup directory")
        return json_response(
            {
                "settings": public,
                "password_set": bool(self.config.get("password")),
                "phases": self.phases,
                "phase_started": self.phase_started,
                "next": jobs,
                "plan_errors": self.plan_errors,
                "history": self.history,
                "local_storage": {
                    "total_count": total_count,
                    "total_size": total_size,
                    "plugin_count": plugin_count,
                    "plugin_size": plugin_size,
                },
            }
        )

    async def settings(self):
        """Validate and persist updated plugin backup settings.

        Returns:
            A JSON response confirming persistence or a 400 error response.
        """
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
        type_map = {"bool": bool, "string": str, "int": int}
        try:
            for key, value in payload.items():
                spec = SCHEMA[key]
                if type(value) is not type_map[spec["type"]]:
                    raise ValueError(f"无效设置项 {key}")
                if isinstance(value, str) and len(value) > 4096:
                    raise ValueError(f"设置值过长 {key}")
                if spec["type"] == "int" and not (0 <= value <= MAX_KEEP_COUNT):
                    raise ValueError(f"保留份数需在 0 到 {MAX_KEEP_COUNT} 之间")
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

    async def cron_preview(self):
        """Preview upcoming trigger times for a Cron expression and IANA timezone.

        Returns:
            A JSON response with the next fire times or a 400 error response.
        """
        payload = await request.json(default=None)
        if not isinstance(payload, dict):
            return error_response("参数无效", status_code=400)
        cron = payload.get("cron")
        tz_name = payload.get("timezone")
        count = payload.get("count", 3)
        if (
            not isinstance(cron, str)
            or not isinstance(tz_name, str)
            or type(count) is not int
            or not (1 <= count <= 5)
        ):
            return error_response("请检查 Cron 表达式、时区或预览数量", status_code=400)
        try:
            tz = ZoneInfo(tz_name.strip())
            trigger = CronTrigger.from_crontab(cron.strip(), timezone=tz)
        except (ValueError, ZoneInfoNotFoundError):
            return error_response("请检查五段 Cron 表达式与 IANA 时区", status_code=400)
        now = datetime.now(tz)
        runs = []
        prev = None
        cursor = now
        for _ in range(count):
            fire = trigger.get_next_fire_time(prev, cursor)
            if fire is None:
                break
            runs.append(fire.isoformat())
            prev = fire
            cursor = fire
        return json_response({"next_runs": runs})

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
            password = payload.get("password") or self.config.get("password", "")
            if password and username and not payload.get("password"):
                # A draft endpoint must not receive saved credentials for a different server.
                saved = self.config.get("url", "")
                if (
                    not saved
                    or URL(collection_url(saved, True)).origin()
                    != URL(collection).origin()
                    or self.config.get("username", "") != username
                ):
                    return error_response(
                        "地址或用户名已更改，请重新输入密码后再测试。",
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

    async def _scheduled(self, mode):
        self._enqueue(mode, trigger="schedule")

    def _enqueue(self, mode, trigger: str = "manual"):
        if self.closing or (mode in self.tasks and not self.tasks[mode].done()):
            return False
        self.phases[mode] = "排队中"
        self.phase_started[mode] = datetime.now(timezone.utc).isoformat()
        self.tasks[mode] = asyncio.create_task(self._backup(mode, trigger=trigger))
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
        if not self._enqueue(mode, trigger="manual"):
            return error_response("任务已排队或正在运行", status_code=409)
        return json_response({"queued": True})

    async def _backup(self, mode, trigger: str = "manual"):
        async with self.lock:
            started = monotonic()
            now = datetime.now(timezone.utc)
            settings = dict(self.config)
            include_temp = bool(settings.get(f"{mode}_items", False))
            record = {
                "mode": mode,
                "trigger": trigger,
                "time": now.isoformat(),
                "include_temp": include_temp,
                "ok": False,
            }
            try:
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
                    "Starting %s backup %s (trigger=%s, include_temp=%s, destination=%s)",
                    mode,
                    name,
                    trigger,
                    include_temp,
                    record.get("remote", backups),
                )
                with tempfile.TemporaryDirectory(
                    prefix=".backup-", dir=backups
                ) as work:
                    path = Path(work) / name
                    self.phases[mode] = "正在打包"
                    await create_archive(
                        data,
                        path,
                        self.context.get_db(),
                        self.context.kb_manager,
                        include_temp=include_temp,
                        plugin_folder=Path(__file__).parent.name,
                    )
                    size = path.stat().st_size
                    logger.info("Created %s archive %s (%d bytes)", mode, name, size)
                    if mode == "local":
                        path.replace(backups / name)
                    else:
                        self.phases[mode] = "正在上传并校验"
                        await upload(
                            path,
                            collection,
                            settings.get("username", ""),
                            settings.get("password", ""),
                        )
                record.update(ok=True, name=name, size=size)
                logger.info("%s backup completed: %s (%d bytes)", mode, name, size)

                keep_count = settings.get(f"{mode}_keep_count", 0)
                if isinstance(keep_count, int) and not isinstance(keep_count, bool) and keep_count > 0:
                    self.phases[mode] = "正在清理旧备份"
                    try:
                        if mode == "local":
                            owned = sorted(
                                (
                                    entry
                                    for entry in backups.iterdir()
                                    if not entry.is_symlink()
                                    and entry.is_file()
                                    and BACKUP_NAME_PATTERNS["local"].fullmatch(
                                        entry.name
                                    )
                                ),
                                key=lambda entry: entry.name,
                                reverse=True,
                            )
                            pruned = 0
                            for old_file in owned[keep_count:]:
                                old_file.unlink(missing_ok=True)
                                pruned += 1
                                logger.info("Pruned old local backup %s", old_file.name)
                        else:
                            pruned = await prune_remote(
                                collection,
                                settings.get("username", ""),
                                settings.get("password", ""),
                                keep_count,
                                BACKUP_NAME_PATTERNS["webdav"],
                            )
                        if pruned:
                            record["pruned"] = pruned
                    except (WebDAVError, OSError) as exc:
                        prune_reason = (
                            str(exc)
                            if isinstance(exc, WebDAVError)
                            else type(exc).__name__
                        )
                        record["prune_error"] = prune_reason
                        logger.warning(
                            "%s backup succeeded, but pruning failed: %s",
                            mode,
                            prune_reason,
                        )
            except Exception as exc:
                reason = (
                    str(exc)
                    if str(exc).startswith(("WebDAV ", "请填写", "知识库管理器"))
                    else type(exc).__name__
                )
                record["error"] = reason
                logger.error(
                    "%s backup failed (destination=%s): %s",
                    mode,
                    record.get("remote", mode),
                    reason,
                )
            finally:
                record["duration_ms"] = int((monotonic() - started) * 1000)
                self.phases[mode] = "空闲"
                self.phase_started[mode] = None
                self.history = [record, *self.history][:30]
                try:
                    temp = self.history_file.with_suffix(".tmp")
                    temp.write_text(
                        json.dumps(self.history, ensure_ascii=False), "utf-8"
                    )
                    temp.replace(self.history_file)
                except OSError:
                    logger.warning("Could not persist backup history")

    async def terminate(self):
        self.closing = True
        if self.scheduler:
            self.scheduler.shutdown(wait=False)
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
