import asyncio
import json
import os
from pathlib import Path
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


def _write_directories(zf, data, include_temp, plugin_folder):
    """Write only selected native directory entries and collect manifest statistics.

    Args:
        zf: Open destination archive, exclusively accessed by this worker.
        data: AstrBot data root.
        include_temp: Whether to include the temporary directory.
        plugin_folder: Installed directory name used by AstrBot for plugin config.

    Returns:
        Native directory statistics keyed by directory name.
    """
    stats = {}
    for name in DIRECTORIES:
        root = data / name
        if (
            (name == "temp" and not include_temp)
            or root.is_symlink()
            or not root.is_dir()
        ):
            continue
        count = size = 0

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
                count += 1
                size += zf.getinfo(member).file_size
        stats[name] = {"files": count, "size": size}
    return stats


async def create_archive(
    data, target, main_db, kb_manager, include_temp: bool, plugin_folder=NAME
):
    """Produce a native ZIP with the standard backup components and optionally temp.

    Args:
        data: AstrBot data directory.
        target: New file in an isolated staging directory outside backed-up trees.
        main_db: Live database owned by the host event loop.
        kb_manager: Host knowledge base manager.
        include_temp: Whether to include the temporary directory.
        plugin_folder: Actual installed folder name.
    """
    if kb_manager is None:
        raise RuntimeError("知识库管理器尚未就绪")
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

        main = await read_tables(main_db, MAIN_DB_MODELS)
        await asyncio.to_thread(write_json, "databases/main_db.json", main)
        metadata = await read_tables(kb_manager.kb_db, KB_METADATA_MODELS)
        await asyncio.to_thread(write_json, "databases/kb_metadata.json", metadata)
        for kb_id, helper in list(kb_manager.kb_insts.items()):
            documents = await exporter._export_kb_documents(helper)
            await asyncio.to_thread(
                write_json, f"databases/kb_{kb_id}/documents.json", documents
            )
            await exporter._export_faiss_index(zf, helper, kb_id)
            await exporter._export_kb_media_files(zf, helper, kb_id)
        for filename in ("cmd_config.json", "mcp_server.json"):
            path = data / filename
            if path.is_file() and not path.is_symlink():
                content = await asyncio.to_thread(path.read_bytes)
                member = f"config/{filename}"
                zf.writestr(member, content)
                exporter._add_checksum(member, content)
        await exporter._export_attachments(zf, main.get("attachments", []))
        stats = await asyncio.to_thread(
            _write_directories, zf, data, include_temp, plugin_folder
        )
        manifest = exporter._generate_manifest(main, metadata, stats)
        manifest["has_config"] = "config/cmd_config.json" in zf.namelist()
        manifest["has_knowledge_bases"] = bool(metadata.get("knowledge_bases"))
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
