import asyncio
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import error_response, json_response, request
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .archive import NAME, create_archive
from .webdav import collection_url, upload

SCHEMA = json.loads(Path(__file__).with_name("_conf_schema.json").read_text("utf-8"))
MODES = ("local", "webdav")


class BackupDashboard(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.scheduler = None
        self.tasks = {}
        self.phases = dict.fromkeys(MODES, "空闲")
        self.lock = asyncio.Lock()
        self.closing = False
        self.history = []
        self.plan_errors = {}
        self.history_file = StarTools.get_data_dir(NAME) / "runs.json"
        for endpoint, method, handler in [
            ("dashboard", "GET", self.dashboard),
            ("settings", "POST", self.settings),
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
        options = SCHEMA["local_items"]
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
                "items": [
                    {"key": k, "name": n, "hint": h}
                    for k, n, h in zip(
                        options["options"],
                        options["labels"],
                        options["subtitles"],
                        strict=True,
                    )
                ],
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
                if (
                    type(value)
                    is not {"bool": bool, "string": str, "list": list}[spec["type"]]
                ):
                    raise ValueError(f"无效设置项 {key}")
                if isinstance(value, str) and len(value) > 4096:
                    raise ValueError(f"设置值过长 {key}")
                if isinstance(value, list) and (
                    any(
                        not isinstance(x, str) or x not in spec["options"]
                        for x in value
                    )
                    or len(value) != len(set(value))
                ):
                    raise ValueError("未知或重复的备份项")
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
                collection_url(candidate["url"], candidate["allow_http"])
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        self.config.save_config(candidate)
        self._apply_plans()
        return json_response({"saved": True})

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
                    self.config.get("url", ""), self.config.get("allow_http", False)
                )
            except ValueError as exc:
                return error_response(str(exc), status_code=400)
        if not self._enqueue(mode):
            return error_response("任务已排队或正在运行", status_code=409)
        return json_response({"queued": True})

    async def _backup(self, mode):
        async with self.lock:
            now = datetime.now(timezone.utc)
            record = {"mode": mode, "time": now.isoformat(), "ok": False}
            try:
                settings = dict(self.config)
                selected = set(
                    settings.get(f"{mode}_items", SCHEMA[f"{mode}_items"]["default"])
                )
                data = Path(get_astrbot_data_path())
                backups = data / "backups"
                backups.mkdir(parents=True, exist_ok=True)
                if mode == "webdav":
                    collection = collection_url(
                        settings.get("url", ""), settings.get("allow_http", False)
                    )
                name = (
                    f"astrbot_backup_{now:%Y%m%d_%H%M%S}_{mode}_{uuid4().hex[:12]}.zip"
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
                        selected,
                        Path(__file__).parent.name,
                    )
                    size = path.stat().st_size
                    if mode == "local":
                        path.replace(backups / name)
                    else:
                        self.phases[mode] = "正在上传"
                        await upload(
                            path,
                            collection,
                            settings.get("username", ""),
                            os.getenv("ASTRBOT_WEBDAV_PASSWORD")
                            or settings.get("password", ""),
                        )
                record.update(ok=True, name=name, size=size)
                logger.info("%s backup completed: %s (%d bytes)", mode, name, size)
            except Exception as exc:
                reason = (
                    str(exc)
                    if str(exc).startswith(("WebDAV ", "请填写", "知识库管理器"))
                    else type(exc).__name__
                )
                record["error"] = reason
                logger.error("%s backup failed: %s", mode, reason)
            finally:
                self.phases[mode] = "空闲"
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
