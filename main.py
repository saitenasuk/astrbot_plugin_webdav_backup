import asyncio
from collections import deque
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from uuid import uuid4
from zipfile import BadZipFile, ZipFile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from yarl import URL

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import (
    error_response,
    file_response,
    json_response,
    request,
    stream_response,
)
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .archive import NAME, create_archive
from .webdav import (
    WebDAVError,
    collection_url,
    delete_remote,
    download_remote,
    list_remote,
    prune_remote,
    upload,
)

SCHEMA = json.loads(Path(__file__).with_name("_conf_schema.json").read_text("utf-8"))
MODES = ("local", "webdav")
NOTIFY_MODES = ("off", "all", "failure")
NOTIFY_MODE_LABELS = {"off": "关闭", "all": "成功 & 失败", "failure": "仅失败"}
MAX_KEEP_COUNT = 1000
MAX_NOTIFY_TARGETS = 50
UMO_PATTERN = re.compile(r"^[^\s:]+:(?:GroupMessage|FriendMessage):[^\s:]+$")
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
        self.progress: dict[str, dict | None] = dict.fromkeys(MODES, None)
        self._subscribers: set[asyncio.Queue[str | None]] = set()
        self.logs: deque[dict] = deque(maxlen=200)
        self._log_seq = 0
        self.lock = asyncio.Lock()
        self.closing = False
        self.history = []
        self.plan_errors = {}
        self.history_file = StarTools.get_data_dir(NAME) / "runs.json"
        for endpoint, method, handler in [
            ("dashboard", "GET", self.dashboard),
            ("events", "GET", self.events),
            ("logs", "GET", self.get_logs),
            ("logs/clear", "POST", self.clear_logs),
            ("temp-size", "GET", self.temp_size),
            ("settings", "POST", self.settings),
            ("cron-preview", "POST", self.cron_preview),
            ("test-connection", "POST", self.test_connection),
            ("notify-test", "POST", self.test_notification),
            ("run", "POST", self.run_now),
            ("archives/local", "GET", self.list_local_archives),
            ("archives/local/download", "GET", self.download_local_archive),
            ("archives/local/delete", "POST", self.delete_local_archive),
            ("archives/webdav", "GET", self.list_webdav_archives),
            ("archives/webdav/pull", "POST", self.pull_webdav_archive),
            ("archives/webdav/delete", "POST", self.delete_webdav_archive),
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
                self._record_log(
                    "WARNING",
                    "system",
                    f"{'本地' if mode == 'local' else 'WebDAV'}定时计划配置无效（{type(exc).__name__}）",
                    f"Invalid schedule for {mode}: {type(exc).__name__}",
                )

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
        platforms = []
        manager = getattr(self.context, "platform_manager", None)
        for inst in getattr(manager, "platform_insts", []) or []:
            meta = inst.meta()
            pid = getattr(meta, "id", "") or ""
            pname = getattr(meta, "name", "") or ""
            if not pid or pname == "webchat":
                continue
            platforms.append(
                {
                    "id": pid,
                    "name": pname,
                    "unsupported": pname
                    in ("qq_official", "qq_official_webhook", "weixin_official_account"),
                }
            )
        return json_response(
            {
                "settings": public,
                "password_set": bool(self.config.get("password")),
                "platforms": platforms,
                "phases": self.phases,
                "phase_started": self.phase_started,
                "progress": self.progress,
                "next": jobs,
                "plan_errors": self.plan_errors,
                "history": self.history,
                "log_count": len(self.logs),
                "local_storage": {
                    "total_count": total_count,
                    "total_size": total_size,
                    "plugin_count": plugin_count,
                    "plugin_size": plugin_size,
                },
            }
        )

    def _record_log(
        self, level: str, category: str, message: str, console_message: str | None = None
    ) -> None:
        """Record a concise operational log entry for both AstrBot console and dashboard UI.

        Args:
            level: Log severity (``INFO``, ``WARNING``, or ``ERROR``).
            category: Logical category (``backup``, ``webdav``, ``archive``, ``notify``, ``system``).
            message: Human-readable message displayed in the dashboard Logs tab.
            console_message: Optional English message written to AstrBot's logger.
        """
        self._log_seq += 1
        entry = {
            "id": self._log_seq,
            "time": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "category": category,
            "message": message,
        }
        self.logs.appendleft(entry)
        text = console_message or message
        if level == "ERROR":
            logger.error("%s", text)
        elif level == "WARNING":
            logger.warning("%s", text)
        else:
            logger.info("%s", text)
        self._broadcast("log", entry)

    async def get_logs(self):
        """Return recent in-memory plugin operational logs newest-first.

        Returns:
            A JSON response containing the log entries list.
        """
        return json_response({"items": list(self.logs)})

    async def clear_logs(self):
        """Clear the in-memory plugin log ring buffer.

        Returns:
            A JSON response confirming the buffer was cleared.
        """
        self.logs.clear()
        return json_response({"cleared": True})

    def _broadcast(self, event_type: str, payload: dict) -> None:
        """Broadcast an SSE event to all active dashboard subscribers.

        Args:
            event_type: SSE event name.
            payload: JSON-serializable event payload.
        """
        if not self._subscribers:
            return
        frame = f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        for queue in list(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                pass

    def _update_progress(
        self, mode: str, phase: str, progress: dict | None = None
    ) -> None:
        """Update in-memory task progress and broadcast it to SSE subscribers.

        Args:
            mode: Destination mode (``local`` or ``webdav``).
            phase: Short human-readable stage title.
            progress: Optional structured progress metadata dict.
        """
        self.phases[mode] = phase
        self.progress[mode] = progress
        self._broadcast(
            "progress",
            {
                "mode": mode,
                "phase": phase,
                "phase_started": self.phase_started[mode],
                "progress": progress,
            },
        )

    async def events(self):
        """Stream live backup progress and completion events over SSE.

        Returns:
            A text/event-stream response emitting snapshot, progress, and completion events.
        """
        queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=64)
        self._subscribers.add(queue)

        async def stream():
            try:
                snapshot = {
                    "phases": dict(self.phases),
                    "phase_started": dict(self.phase_started),
                    "progress": dict(self.progress),
                }
                yield f"event: snapshot\ndata: {json.dumps(snapshot, ensure_ascii=False)}\n\n"
                while not self.closing:
                    try:
                        frame = await asyncio.wait_for(queue.get(), timeout=25.0)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if frame is None:
                        break
                    yield frame
            finally:
                self._subscribers.discard(queue)

        return stream_response(
            stream(),
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    async def temp_size(self):
        """Measure the current size and file count of AstrBot's data/temp directory.

        Uses the same traversal exclusions as archive creation so the estimate
        reflects what would actually be included when backing up ``temp``.

        Returns:
            A JSON response containing ``exists``, ``size``, and ``files``.
        """
        temp_dir = Path(get_astrbot_data_path()) / "temp"

        def _scan() -> dict:
            if temp_dir.is_symlink() or not temp_dir.is_dir():
                return {"exists": False, "size": 0, "files": 0}
            total_size = 0
            total_files = 0
            for folder, dirs, files in os.walk(temp_dir):
                parent = Path(folder)
                dirs[:] = [
                    d
                    for d in dirs
                    if d not in (".git", "__pycache__")
                    and not (parent / d).is_symlink()
                ]
                for file in files:
                    if file.endswith(".pyc"):
                        continue
                    path = parent / file
                    if path.is_symlink() or not path.is_file():
                        continue
                    try:
                        total_size += path.stat().st_size
                        total_files += 1
                    except OSError:
                        continue
            return {"exists": True, "size": total_size, "files": total_files}

        try:
            result = await asyncio.to_thread(_scan)
        except OSError:
            return error_response("无法读取 data/temp 目录", status_code=500)
        return json_response(result)

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
        type_map = {"bool": bool, "string": str, "int": int, "list": list}
        try:
            for key, value in payload.items():
                spec = SCHEMA[key]
                if type(value) is not type_map[spec["type"]]:
                    raise ValueError(f"无效设置项 {key}")
                if isinstance(value, str) and len(value) > 4096:
                    raise ValueError(f"设置值过长 {key}")
                if spec["type"] == "int" and not (0 <= value <= MAX_KEEP_COUNT):
                    raise ValueError(f"保留份数需在 0 到 {MAX_KEEP_COUNT} 之间")
                if key == "notify_mode" and value not in NOTIFY_MODES:
                    raise ValueError("通知时机选项无效")
                if key == "notify_targets":
                    value = self._validate_notify_targets(value)
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

    @staticmethod
    def _validate_notify_targets(raw: object) -> list[str]:
        """Validate and deduplicate a list of unified_msg_origin targets.

        Args:
            raw: Candidate list of unified_msg_origin strings.

        Returns:
            Deduplicated list of validated unified_msg_origin strings.

        Raises:
            ValueError: If any entry is malformed or the list exceeds limits.
        """
        if not isinstance(raw, list):
            raise ValueError("通知目标必须是列表")
        if len(raw) > MAX_NOTIFY_TARGETS:
            raise ValueError(f"通知目标数量不能超过 {MAX_NOTIFY_TARGETS} 个")
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, str):
                raise ValueError("通知目标格式无效")
            umo = item.strip()
            if (
                not umo
                or len(umo) > 256
                or any(ord(ch) < 32 for ch in umo)
                or not UMO_PATTERN.fullmatch(umo)
            ):
                raise ValueError(
                    f"通知目标格式无效：{umo or '(空)'}（应为 平台ID:GroupMessage/FriendMessage:会话ID）"
                )
            if umo not in seen:
                seen.add(umo)
                cleaned.append(umo)
        return cleaned

    @staticmethod
    def _format_size(size: int) -> str:
        """Format a byte count into a concise human-readable string.

        Args:
            size: File size in bytes.

        Returns:
            Formatted size string.
        """
        if size < 1024:
            return f"{size} B"
        if size < 1048576:
            return f"{size / 1024:.1f} KiB"
        if size < 1073741824:
            return f"{size / 1048576:.1f} MiB"
        return f"{size / 1073741824:.2f} GiB"

    async def _send_backup_notification(self, record: dict) -> None:
        """Send a backup result notification to configured sessions when enabled.

        Args:
            record: Completed backup run record dictionary.
        """
        notify_mode = self.config.get("notify_mode", "off")
        if notify_mode == "off":
            return
        if notify_mode == "failure" and record.get("ok"):
            return
        raw_targets = self.config.get("notify_targets") or []
        if not isinstance(raw_targets, list) or not raw_targets:
            return

        dest_label = "本地" if record.get("mode") == "local" else "WebDAV"
        trigger_label = "定时" if record.get("trigger") == "schedule" else "手动"
        duration_ms = record.get("duration_ms")
        dur_text = (
            f"{duration_ms / 1000:.1f} s"
            if isinstance(duration_ms, int) and duration_ms >= 0
            else ""
        )
        if record.get("ok"):
            size_text = self._format_size(int(record.get("size") or 0))
            lines = [
                f"[AstrBot 备份] {dest_label}{trigger_label}备份成功",
                f"归档大小：{size_text}" + (f" ｜ 耗时：{dur_text}" if dur_text else ""),
            ]
            if record.get("pruned"):
                lines.append(f"已自动清理 {record['pruned']} 份旧备份")
            if record.get("prune_error"):
                lines.append("警告：旧备份自动清理失败，请前往仪表盘查看详情")
        else:
            lines = [
                f"[AstrBot 备份] {dest_label}{trigger_label}备份失败",
                f"原因：{record.get('error') or '未知错误'}",
                "请前往 AstrBot 备份仪表盘排查。",
            ]
        text = "\n".join(lines)

        for target in raw_targets:
            if not isinstance(target, str) or not target.strip():
                continue
            try:
                matched = await self.context.send_message(
                    target.strip(), MessageChain().message(text)
                )
                if not matched:
                    self._record_log(
                        "WARNING",
                        "notify",
                        f"通知发送未匹配到运行中的平台：{target}",
                        f"Backup notification target platform not found: {target}",
                    )
            except Exception as exc:
                self._record_log(
                    "WARNING",
                    "notify",
                    f"通知发送失败（{target}）：{exc}",
                    f"Failed to send backup notification to {target}: {exc}",
                )

    async def test_notification(self):
        """Send a test notification message to the submitted or saved target sessions.

        Returns:
            A JSON response summarizing delivery counts, or an error response.
        """
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("参数无效", status_code=400)
        raw_targets = (
            payload["targets"]
            if "targets" in payload
            else self.config.get("notify_targets", [])
        )
        try:
            targets = self._validate_notify_targets(raw_targets)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        if not targets:
            return error_response("请先添加至少一个通知目标会话", status_code=400)

        sent = 0
        failed: list[str] = []
        message = "[AstrBot 备份] 这是一条测试通知，说明当前会话可以正常接收备份结果推送。"
        for target in targets:
            try:
                matched = await self.context.send_message(
                    target, MessageChain().message(message)
                )
                if matched:
                    sent += 1
                else:
                    failed.append(f"{target}（未找到对应运行中的消息平台）")
            except Exception as exc:
                failed.append(f"{target}（{type(exc).__name__}: {exc}）")

        if sent == 0:
            return error_response(
                "测试通知发送失败：" + "；".join(failed), status_code=502
            )
        return json_response({"sent": sent, "failed": failed})

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("backup_notify")
    async def cmd_backup_notify(self, event: AstrMessageEvent, action: str = "", value: str = ""):
        """Bind or manage backup notifications for the current chat session.

        Args:
            event: Incoming AstrBot message event.
            action: Subcommand (``add``, ``del``, ``list``, or ``mode``).
            value: Mode value when ``action`` is ``mode``.
        """
        action = action.strip().lower()
        umo = getattr(event, "unified_msg_origin", "") or ""
        targets = list(self.config.get("notify_targets") or [])
        cur_mode = self.config.get("notify_mode", "off")

        if action == "add":
            if not umo or not UMO_PATTERN.fullmatch(umo):
                yield event.plain_result("当前会话不支持绑定备份通知。")
                return
            if umo in targets:
                yield event.plain_result(
                    f"当前会话已在通知列表中（通知时机：{NOTIFY_MODE_LABELS.get(cur_mode, cur_mode)}）。"
                )
                return
            if len(targets) >= MAX_NOTIFY_TARGETS:
                yield event.plain_result(f"通知目标数量已达上限（{MAX_NOTIFY_TARGETS}）。")
                return
            targets.append(umo)
            self.config.save_config({"notify_targets": targets})
            tip = (
                " 当前通知时机为「关闭」，可发送 /backup_notify mode failure 或在仪表盘开启。"
                if cur_mode == "off"
                else ""
            )
            yield event.plain_result(f"已将当前会话加入备份通知列表：{umo}{tip}")
            return

        if action in ("del", "remove"):
            if umo not in targets:
                yield event.plain_result("当前会话不在备份通知列表中。")
                return
            targets = [item for item in targets if item != umo]
            self.config.save_config({"notify_targets": targets})
            yield event.plain_result(f"已将当前会话移出备份通知列表：{umo}")
            return

        if action == "mode":
            next_mode = value.strip().lower()
            if next_mode not in NOTIFY_MODES:
                yield event.plain_result(
                    "无效的通知时机。用法：/backup_notify mode off|all|failure"
                )
                return
            self.config.save_config({"notify_mode": next_mode})
            yield event.plain_result(
                f"备份通知时机已设置为：{NOTIFY_MODE_LABELS[next_mode]}"
            )
            return

        if action == "list":
            bound = "已绑定" if umo in targets else "未绑定"
            yield event.plain_result(
                f"备份通知时机：{NOTIFY_MODE_LABELS.get(cur_mode, cur_mode)}\n"
                f"已绑定目标数：{len(targets)}（当前会话：{bound}）"
            )
            return

        yield event.plain_result(
            "备份通知管理指令（仅管理员）：\n"
            "/backup_notify add — 绑定当前群/私聊接收通知\n"
            "/backup_notify del — 移除当前群/私聊\n"
            "/backup_notify list — 查看当前通知状态\n"
            "/backup_notify mode off|all|failure — 设置通知时机"
        )

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
            self._record_log(
                "INFO",
                "webdav",
                "WebDAV 连接测试通过（写入、校验并清理成功）",
                "WebDAV connection test completed",
            )
            return json_response({"verified": True})
        except (WebDAVError, OSError) as exc:
            reason = str(exc) if isinstance(exc, WebDAVError) else type(exc).__name__
            self._record_log(
                "WARNING",
                "webdav",
                f"WebDAV 连接测试失败：{reason}",
                f"WebDAV connection test failed: {reason}",
            )
            return error_response(reason, status_code=502)

    async def _scheduled(self, mode):
        self._enqueue(mode, trigger="schedule")

    def _enqueue(self, mode, trigger: str = "manual"):
        if self.closing or (mode in self.tasks and not self.tasks[mode].done()):
            return False
        self.phase_started[mode] = datetime.now(timezone.utc).isoformat()
        self._update_progress(
            mode,
            "排队中",
            {
                "stage": "queue",
                "detail": "任务已进入队列，等待执行…",
                "determinate": False,
            },
        )
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

    @staticmethod
    def _validate_zip_name(name: object) -> str:
        """Validate a single-segment ZIP archive filename from user input.

        Args:
            name: Candidate filename value from request query or JSON body.

        Returns:
            Normalized safe ZIP filename.

        Raises:
            ValueError: If the filename is unsafe or not a ZIP archive.
        """
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 255
            or not name.endswith(".zip")
            or "/" in name
            or "\\" in name
            or name in (".", "..")
            or Path(name).name != name
            or any(ord(ch) < 32 for ch in name)
        ):
            raise ValueError("备份文件名无效")
        return name

    async def list_local_archives(self):
        """List ZIP archives stored in AstrBot's local backups directory.

        Returns:
            A JSON response with archive metadata sorted newest-first.
        """
        backups_dir = Path(get_astrbot_data_path()) / "backups"
        items = []
        total_size = 0
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
                        stat = entry.stat()
                    except OSError:
                        continue
                    total_size += stat.st_size
                    items.append(
                        {
                            "name": entry.name,
                            "size": stat.st_size,
                            "modified": datetime.fromtimestamp(
                                stat.st_mtime, tz=timezone.utc
                            ).isoformat(),
                            "owned": bool(
                                BACKUP_NAME_PATTERNS["local"].fullmatch(entry.name)
                            ),
                        }
                    )
            except OSError:
                return error_response("无法读取本地备份目录", status_code=500)
        items.sort(key=lambda item: (item["modified"], item["name"]), reverse=True)
        return json_response({"items": items, "total_size": total_size})

    async def download_local_archive(self):
        """Download a validated local backup ZIP via AstrBot file_response.

        Returns:
            A file download response or an error response if missing/invalid.
        """
        try:
            name = self._validate_zip_name(request.query.get("name", ""))
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        backups_dir = (Path(get_astrbot_data_path()) / "backups").resolve()
        target = (backups_dir / name).resolve()
        if (
            target.parent != backups_dir
            or target.is_symlink()
            or not target.is_file()
        ):
            return error_response("本地备份文件不存在", status_code=404)
        return file_response(target, filename=name, content_type="application/zip")

    async def delete_local_archive(self):
        """Delete a validated local backup ZIP from data/backups.

        Returns:
            A JSON response confirming deletion or an error response.
        """
        payload = await request.json(default=None)
        raw_name = payload.get("name") if isinstance(payload, dict) else None
        try:
            name = self._validate_zip_name(raw_name)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        backups_dir = (Path(get_astrbot_data_path()) / "backups").resolve()
        target = (backups_dir / name).resolve()
        if (
            target.parent != backups_dir
            or target.is_symlink()
            or not target.is_file()
        ):
            return error_response("本地备份文件不存在", status_code=404)
        try:
            target.unlink()
        except OSError as exc:
            self._record_log(
                "WARNING",
                "archive",
                f"删除本地备份失败（{name}）：{exc}",
                f"Failed to delete local backup {name}: {exc}",
            )
            return error_response("删除本地备份失败", status_code=500)
        self._record_log(
            "INFO",
            "archive",
            f"已删除本地备份 {name}",
            f"Deleted local backup {name}",
        )
        return json_response({"deleted": True, "name": name})

    async def list_webdav_archives(self):
        """List ZIP archives in the configured WebDAV remote directory.

        Returns:
            A JSON response containing remote ZIP metadata or an error response.
        """
        try:
            collection = collection_url(
                self.config.get("url", ""),
                self.config.get("allow_http", False),
                self.config.get("remote_dir", "/"),
            )
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        try:
            raw_items = await list_remote(
                collection,
                self.config.get("username", ""),
                self.config.get("password", ""),
            )
        except (WebDAVError, OSError) as exc:
            reason = str(exc) if isinstance(exc, WebDAVError) else type(exc).__name__
            self._record_log(
                "WARNING",
                "webdav",
                f"读取 WebDAV 远端文件列表失败：{reason}",
                f"WebDAV list failed: {reason}",
            )
            return error_response(reason, status_code=502)
        backups_dir = Path(get_astrbot_data_path()) / "backups"
        total_size = 0
        items = []
        for item in raw_items:
            if isinstance(item["size"], int):
                total_size += item["size"]
            items.append(
                {
                    "name": item["name"],
                    "size": item["size"],
                    "modified": item["modified"],
                    "owned": bool(
                        BACKUP_NAME_PATTERNS["webdav"].fullmatch(item["name"])
                    ),
                    "exists_local": (backups_dir / item["name"]).is_file(),
                }
            )
        return json_response(
            {"items": items, "total_size": total_size, "collection": collection}
        )

    async def pull_webdav_archive(self):
        """Pull a remote WebDAV backup ZIP into local data/backups for native restore.

        Returns:
            A JSON response with the saved local filename and size, or an error.
        """
        payload = await request.json(default=None)
        raw_name = payload.get("name") if isinstance(payload, dict) else None
        try:
            name = self._validate_zip_name(raw_name)
            collection = collection_url(
                self.config.get("url", ""),
                self.config.get("allow_http", False),
                self.config.get("remote_dir", "/"),
            )
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        backups_dir = Path(get_astrbot_data_path()) / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)
        final_path = backups_dir / name
        if final_path.exists() or final_path.is_symlink():
            return error_response("本地已存在同名备份文件", status_code=409)
        try:
            with tempfile.TemporaryDirectory(prefix=".pull-", dir=backups_dir) as work:
                staged = Path(work) / name
                size = await download_remote(
                    collection,
                    name,
                    staged,
                    self.config.get("username", ""),
                    self.config.get("password", ""),
                    allow_http=bool(self.config.get("allow_http", False)),
                )

                def _verify_zip(path: Path) -> None:
                    with ZipFile(path, "r") as zf:
                        if not zf.namelist():
                            raise BadZipFile("empty archive")

                await asyncio.to_thread(_verify_zip, staged)
                staged.replace(final_path)
        except BadZipFile:
            self._record_log(
                "WARNING",
                "archive",
                f"拉取远端备份失败（{name}）：文件不是有效的 ZIP 包",
                f"WebDAV pull rejected invalid ZIP {name}",
            )
            return error_response("远端文件不是有效的 ZIP 备份包", status_code=502)
        except (WebDAVError, OSError) as exc:
            reason = str(exc) if isinstance(exc, WebDAVError) else type(exc).__name__
            self._record_log(
                "WARNING",
                "archive",
                f"拉取远端备份失败（{name}）：{reason}",
                f"WebDAV pull failed for {name}: {reason}",
            )
            return error_response(reason, status_code=502)
        self._record_log(
            "INFO",
            "archive",
            f"已将远端备份拉取到本地：{name}（{self._format_size(size)}）",
            f"Pulled remote backup {name} to local ({self._format_size(size)})",
        )
        return json_response({"pulled": True, "name": name, "size": size})

    async def delete_webdav_archive(self):
        """Delete a single validated ZIP archive from the WebDAV remote directory.

        Returns:
            A JSON response confirming remote deletion or an error response.
        """
        payload = await request.json(default=None)
        raw_name = payload.get("name") if isinstance(payload, dict) else None
        try:
            name = self._validate_zip_name(raw_name)
            collection = collection_url(
                self.config.get("url", ""),
                self.config.get("allow_http", False),
                self.config.get("remote_dir", "/"),
            )
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        try:
            await delete_remote(
                collection,
                name,
                self.config.get("username", ""),
                self.config.get("password", ""),
            )
        except (WebDAVError, OSError) as exc:
            reason = str(exc) if isinstance(exc, WebDAVError) else type(exc).__name__
            self._record_log(
                "WARNING",
                "archive",
                f"删除远端备份失败（{name}）：{reason}",
                f"WebDAV delete failed for {name}: {reason}",
            )
            return error_response(reason, status_code=502)
        self._record_log(
            "INFO",
            "archive",
            f"已删除远端备份 {name}",
            f"Deleted remote backup {name}",
        )
        return json_response({"deleted": True, "name": name})

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
                mode_label = "本地" if mode == "local" else "WebDAV"
                trigger_label = "定时" if trigger == "schedule" else "手动"
                self._record_log(
                    "INFO",
                    "backup",
                    f"开始{mode_label}{trigger_label}备份（{'含 temp' if include_temp else '不含 temp'}）",
                    f"Starting {mode} backup ({trigger}, include_temp={include_temp})",
                )
                with tempfile.TemporaryDirectory(
                    prefix=".backup-", dir=backups
                ) as work:
                    path = Path(work) / name
                    self._update_progress(
                        mode,
                        "正在打包",
                        {
                            "stage": "database",
                            "detail": "准备打包备份内容…",
                            "percent": 0.0,
                            "determinate": True,
                        },
                    )
                    await create_archive(
                        data,
                        path,
                        self.context.get_db(),
                        self.context.kb_manager,
                        include_temp=include_temp,
                        plugin_folder=Path(__file__).parent.name,
                        on_progress=lambda info: self._update_progress(
                            mode, "正在打包", info
                        ),
                    )
                    size = path.stat().st_size
                    logger.debug("Created %s archive %s (%d bytes)", mode, name, size)
                    if mode == "local":
                        path.replace(backups / name)
                    else:
                        self._update_progress(
                            mode,
                            "正在上传",
                            {
                                "stage": "upload",
                                "detail": "准备上传备份包到 WebDAV…",
                                "percent": 0.0,
                                "determinate": True,
                                "current": 0,
                                "total": size,
                                "rate_bps": 0,
                            },
                        )
                        stage_titles = {
                            "upload": "正在上传",
                            "commit": "等待服务端写入",
                            "verify": "正在校验远端",
                        }
                        await upload(
                            path,
                            collection,
                            settings.get("username", ""),
                            settings.get("password", ""),
                            on_progress=lambda info: self._update_progress(
                                mode,
                                stage_titles.get(info.get("stage", ""), "正在上传"),
                                info,
                            ),
                        )
                record.update(ok=True, name=name, size=size)

                keep_count = settings.get(f"{mode}_keep_count", 0)
                if (
                    isinstance(keep_count, int)
                    and not isinstance(keep_count, bool)
                    and keep_count > 0
                ):
                    self._update_progress(
                        mode,
                        "正在清理旧备份",
                        {
                            "stage": "prune",
                            "detail": f"正在按保留策略（最多 {keep_count} 份）清理旧备份…",
                            "determinate": False,
                        },
                    )
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
                                logger.debug("Pruned old local backup %s", old_file.name)
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
                        self._record_log(
                            "WARNING",
                            "backup",
                            f"{mode_label}备份已成功，但清理旧备份失败：{prune_reason}",
                            f"{mode} backup succeeded, but pruning failed: {prune_reason}",
                        )
                elapsed_s = monotonic() - started
                pruned_note_zh = (
                    f"，已清理 {record['pruned']} 份旧备份"
                    if record.get("pruned")
                    else ""
                )
                pruned_note_en = (
                    f", pruned {record['pruned']} old backup(s)"
                    if record.get("pruned")
                    else ""
                )
                self._record_log(
                    "INFO",
                    "backup",
                    f"{mode_label}备份完成：{name}（{self._format_size(size)}，耗时 {elapsed_s:.1f} s{pruned_note_zh}）",
                    f"{mode} backup completed: {name} ({self._format_size(size)}, {elapsed_s:.1f}s{pruned_note_en})",
                )
            except Exception as exc:
                reason = (
                    str(exc)
                    if str(exc).startswith(("WebDAV ", "请填写", "知识库管理器"))
                    else type(exc).__name__
                )
                record["error"] = reason
                mode_label = "本地" if mode == "local" else "WebDAV"
                self._record_log(
                    "ERROR",
                    "backup",
                    f"{mode_label}备份失败：{reason}",
                    f"{mode} backup failed: {reason}",
                )
            finally:
                record["duration_ms"] = int((monotonic() - started) * 1000)
                self.phases[mode] = "空闲"
                self.phase_started[mode] = None
                self.progress[mode] = None
                self.history = [record, *self.history][:30]
                try:
                    temp = self.history_file.with_suffix(".tmp")
                    temp.write_text(
                        json.dumps(self.history, ensure_ascii=False), "utf-8"
                    )
                    temp.replace(self.history_file)
                except OSError:
                    logger.warning("Could not persist backup history")
                self._broadcast("completed", {"mode": mode, "record": record})
                await self._send_backup_notification(record)

    async def terminate(self):
        self.closing = True
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
        if self.scheduler:
            self.scheduler.shutdown(wait=False)
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
