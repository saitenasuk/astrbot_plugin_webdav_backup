import asyncio
import json
import os
from pathlib import Path
from time import monotonic
from zipfile import ZIP_DEFLATED, ZipFile

from sqlalchemy import select, text

from astrbot.core.backup.constants import KB_METADATA_MODELS, MAIN_DB_MODELS
from astrbot.core.backup.exporter import AstrBotExporter

NAME = "astrbot_plugin_webdav_backup"
DIRECTORIES = (
    "config",
    "plugins",
    "plugin_data",
    "webchat",
    "skills",
    "t2i_templates",
    "temp",
)


def _write_directories(zf, data, include_temp, plugin_folder, on_progress=None):
    """Write only selected native directory entries and collect manifest statistics.

    Args:
        zf: Open destination archive, exclusively accessed by this worker.
        data: AstrBot data root.
        include_temp: Whether to include the temporary directory.
        plugin_folder: Installed directory name used by AstrBot for plugin config.
        on_progress: Optional thread-safe callback receiving progress dicts.

    Returns:
        Native directory statistics keyed by directory name.
    """
    stats = {}
    active_dirs = [
        name
        for name in DIRECTORIES
        if not (name == "temp" and not include_temp)
        and not (data / name).is_symlink()
        and (data / name).is_dir()
    ]
    total_dirs = len(active_dirs) or 1
    total_files = 0
    total_bytes = 0
    last_emit = 0.0

    for idx, name in enumerate(active_dirs):
        root = data / name
        count = size = 0
        base_pct = 45 + (idx / total_dirs) * 50

        if on_progress:
            last_emit = monotonic()
            on_progress(
                {
                    "stage": "directories",
                    "detail": f"正在打包目录 {name}（{idx + 1}/{len(active_dirs)}）…",
                    "percent": round(base_pct, 1),
                    "determinate": True,
                    "files": total_files,
                    "bytes": total_bytes,
                }
            )

        def raise_walk_error(err):
            raise err

        for folder, dirs, files in os.walk(root, onerror=raise_walk_error):
            parent = Path(folder)
            dirs[:] = [
                d
                for d in dirs
                if d not in (".git", "__pycache__") and not (parent / d).is_symlink()
            ]
            for file in files:
                path = parent / file
                if file.endswith(".pyc") or path.is_symlink() or not path.is_file():
                    continue
                member = f"directories/{name}/{path.relative_to(root).as_posix()}"
                own_config = (
                    name == "config"
                    and parent == root
                    and any(
                        file.startswith(f"{prefix}_config.json")
                        for prefix in {NAME, plugin_folder}
                    )
                )
                if own_config:
                    if not file.endswith("_config.json"):
                        continue
                    config = json.loads(path.read_bytes())
                    config["password"] = ""
                    content = json.dumps(config, ensure_ascii=False, indent=2).encode()
                    zf.writestr(member, content)
                else:
                    zf.write(path, member)
                file_size = zf.getinfo(member).file_size
                count += 1
                size += file_size
                total_files += 1
                total_bytes += file_size
                if on_progress:
                    now = monotonic()
                    if now - last_emit >= 0.25:
                        last_emit = now
                        on_progress(
                            {
                                "stage": "directories",
                                "detail": f"正在打包目录 {name}（已写入 {total_files} 个文件）…",
                                "percent": round(base_pct, 1),
                                "determinate": True,
                                "files": total_files,
                                "bytes": total_bytes,
                            }
                        )
        stats[name] = {"files": count, "size": size}
    return stats


async def create_archive(
    data,
    target,
    main_db,
    kb_manager,
    include_temp: bool,
    plugin_folder=NAME,
    on_progress=None,
):
    """Produce a native ZIP with the standard backup components and optionally temp.

    Args:
        data: AstrBot data directory.
        target: New file in an isolated staging directory outside backed-up trees.
        main_db: Live database owned by the host event loop.
        kb_manager: Host knowledge base manager.
        include_temp: Whether to include the temporary directory.
        plugin_folder: Actual installed folder name.
        on_progress: Optional callback invoked with progress payload dicts.
    """
    if kb_manager is None:
        raise RuntimeError("知识库管理器尚未就绪")

    def emit(payload: dict):
        if on_progress:
            on_progress(payload)

    exporter = AstrBotExporter(main_db, kb_manager)
    with ZipFile(target, "x", compression=ZIP_DEFLATED) as zf:

        def write_json(member, value):
            raw = json.dumps(value, ensure_ascii=False, indent=2, default=str)
            zf.writestr(member, raw)
            exporter._add_checksum(member, raw)

        async def read_tables(db, models):
            # An unreadable table must fail the job. Native restore replaces the DB.
            result = {}
            async with db.get_db() as session:
                await session.execute(text("BEGIN"))
                for name, model in models.items():
                    rows = await session.execute(select(model))
                    result[name] = [
                        exporter._model_to_dict(row) for row in rows.scalars()
                    ]
            return result

        emit(
            {
                "stage": "database",
                "detail": "正在导出主数据库…",
                "percent": 5.0,
                "determinate": True,
            }
        )
        main = await read_tables(main_db, MAIN_DB_MODELS)
        await asyncio.to_thread(write_json, "databases/main_db.json", main)

        emit(
            {
                "stage": "kb",
                "detail": "正在导出知识库元数据…",
                "percent": 15.0,
                "determinate": True,
            }
        )
        metadata = await read_tables(kb_manager.kb_db, KB_METADATA_MODELS)
        await asyncio.to_thread(write_json, "databases/kb_metadata.json", metadata)

        kb_items = list(kb_manager.kb_insts.items())
        for idx, (kb_id, helper) in enumerate(kb_items, start=1):
            emit(
                {
                    "stage": "kb",
                    "detail": f"正在导出知识库（{idx}/{len(kb_items)}）…",
                    "percent": round(15.0 + (idx / max(len(kb_items), 1)) * 15.0, 1),
                    "determinate": True,
                }
            )
            documents = await exporter._export_kb_documents(helper)
            await asyncio.to_thread(
                write_json, f"databases/kb_{kb_id}/documents.json", documents
            )
            await exporter._export_faiss_index(zf, helper, kb_id)
            await exporter._export_kb_media_files(zf, helper, kb_id)

        emit(
            {
                "stage": "config",
                "detail": "正在导出配置文件…",
                "percent": 32.0,
                "determinate": True,
            }
        )
        for filename in ("cmd_config.json", "mcp_server.json"):
            path = data / filename
            if path.is_file() and not path.is_symlink():
                content = await asyncio.to_thread(path.read_bytes)
                member = f"config/{filename}"
                zf.writestr(member, content)
                exporter._add_checksum(member, content)

        attachments = main.get("attachments", [])
        emit(
            {
                "stage": "attachments",
                "detail": (
                    f"正在导出附件文件（{len(attachments)} 条记录）…"
                    if attachments
                    else "正在检查附件文件…"
                ),
                "percent": 40.0,
                "determinate": True,
            }
        )
        await exporter._export_attachments(zf, attachments)

        loop = asyncio.get_running_loop()
        dir_progress = (
            (lambda payload: loop.call_soon_threadsafe(on_progress, payload))
            if on_progress
            else None
        )
        stats = await asyncio.to_thread(
            _write_directories, zf, data, include_temp, plugin_folder, dir_progress
        )
        if on_progress:
            await asyncio.sleep(0)

        emit(
            {
                "stage": "manifest",
                "detail": "正在生成清单并完成打包…",
                "percent": 98.0,
                "determinate": True,
            }
        )
        manifest = exporter._generate_manifest(main, metadata, stats)
        manifest["has_config"] = "config/cmd_config.json" in zf.namelist()
        manifest["has_knowledge_bases"] = bool(metadata.get("knowledge_bases"))
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
