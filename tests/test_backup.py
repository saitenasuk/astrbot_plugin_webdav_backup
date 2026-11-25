import asyncio
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zipfile import ZipFile

from aiohttp import web
from aiohttp.test_utils import TestServer
from astrbot.core.backup.importer import AstrBotImporter
from astrbot.core.db.sqlite import SQLiteDatabase
from astrbot.core.knowledge_base.kb_db_sqlite import KBSQLiteDatabase

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
plugin = importlib.import_module(f'{ROOT.name}.main')


class BackupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        for folder in ['temp','skills','config','remote','restored']:
            (self.data / folder).mkdir()
        (self.data/'temp/media.jpg').write_bytes(b'temporary')
        (self.data/'skills/guide.md').write_text('keep')
        (self.data/'config'/f'{plugin.NAME}_config.json').write_text(json.dumps({'password':'private','local_enabled':True}))
        self.db = SQLiteDatabase(str(self.data/'db.sqlite'))
        await self.db.initialize()
        self.addAsyncCleanup(self.db.engine.dispose)
        await self.db.insert_persona('backup-test','keep this prompt')
        class Config(dict):
            def save_config(self, candidate):
                self.update(candidate)
        self.config = Config({k:v['default'] for k,v in plugin.SCHEMA.items()})
        kb = KBSQLiteDatabase(str(self.data / 'knowledge_base/kb.db'))
        await kb.initialize()
        self.addAsyncCleanup(kb.engine.dispose)
        self.kb_manager = SimpleNamespace(kb_db=kb, kb_insts={})
        self.config.update(password='private')
        with patch.object(plugin.StarTools,'get_data_dir',return_value=self.data):
            self.bot=plugin.BackupDashboard(MagicMock(get_db=lambda:self.db,kb_manager=self.kb_manager),self.config)
        await self.bot.initialize()
        self.addAsyncCleanup(self.bot.terminate)
        data_patch=patch.object(plugin,'get_astrbot_data_path',return_value=str(self.data))
        data_patch.start(); self.addCleanup(data_patch.stop)

    async def test_local_native_restore(self):
        self.assertTrue(self.bot._enqueue('local'))
        self.assertFalse(self.bot._enqueue('local'))
        await self.bot.tasks['local']
        backup, = (self.data/'backups').glob('*.zip')
        with ZipFile(backup) as z:
            self.assertEqual(z.testzip(),None)
            self.assertIn('databases/kb_metadata.json', z.namelist())
            self.assertNotIn('directories/temp/media.jpg',z.namelist())
            self.assertNotIn(b'private',z.read(f'directories/config/{plugin.NAME}_config.json'))
            self.assertEqual(set(json.loads(z.read('manifest.json'))['directories']),{'config','skills'})
        dest=self.data/'restored'
        target=SQLiteDatabase(str(dest/'dst.db'))
        await target.initialize()
        self.addAsyncCleanup(target.engine.dispose)
        importer=AstrBotImporter(target,config_path=str(dest/'cmd_config.json'))
        self.assertTrue(importer.pre_check(str(backup)).can_import)
        with patch('astrbot.core.backup.importer.get_backup_directories', return_value={'config': str(dest/'config'),'skills':str(dest/'skills')}):
            result = await importer.import_all(str(backup))
        self.assertTrue(result.success,result.errors)
        self.assertEqual((await target.get_persona_by_id('backup-test')).system_prompt,'keep this prompt')
        self.assertEqual((dest/'skills/guide.md').read_text(),'keep')

    async def test_local_scope_with_and_without_temp(self):
        for name in ('plugins', 'plugin_data', 'webchat', 't2i_templates'):
            (self.data / name).mkdir()
            (self.data / name / 'example.txt').write_text(name)
        directories = {'config', 'skills', 'plugins', 'plugin_data', 'webchat', 't2i_templates'}
        (self.data / 'attachments').mkdir()
        attachment = self.data / 'attachments' / 'sample.bin'
        attachment.write_bytes(b'attachment contents')
        await self.db.insert_attachment(str(attachment), 'file', 'application/octet-stream')
        for include_temp in (True, False):
            payload = SimpleNamespace(json=AsyncMock(return_value={'local_items': include_temp}))
            with patch.object(plugin, 'request', payload):
                self.assertEqual((await self.bot.settings()).status_code, 200)
            self.assertIs(self.config['webdav_items'], False)
            await self.bot._backup('local')
            record = self.bot.history[0]
            self.assertTrue(record['ok'], record)
            with ZipFile(self.data / 'backups' / record['name']) as archive:
                manifest = json.loads(archive.read('manifest.json'))
                self.assertEqual(set(manifest['directories']), directories | ({'temp'} if include_temp else set()))
                self.assertEqual('directories/temp/media.jpg' in archive.namelist(), include_temp)
                self.assertIn('databases/main_db.json', archive.namelist())
                self.assertIn('databases/kb_metadata.json', archive.namelist())
                self.assertTrue(any(name.startswith('files/attachments/') for name in archive.namelist()))

    async def test_upgrade_keeps_previous_temp_choice(self):
        for local, webdav, expected in [
            (['temp'], [], (True, False)),
            ([], ['config', 'temp'], (False, True)),
            (True, ['config'], (True, False)),
            (False, True, (False, True)),
        ]:
            self.config.update(local_items=local, webdav_items=webdav)
            with patch.object(plugin.StarTools, 'get_data_dir', return_value=self.data):
                plugin.BackupDashboard(MagicMock(), self.config)
            self.assertIs(self.config['local_items'], expected[0])
            self.assertIs(self.config['webdav_items'], expected[1])

    async def test_webdav_put_move_and_independent_selection(self):
        remote=self.data/'remote'
        async def handle(request):
            self.assertIsNotNone(request.headers.get('Authorization'))
            path=remote/request.match_info['name']
            if request.method=='PUT':
                path.write_bytes(await request.read()); return web.Response(status=201)
            self.assertEqual(request.method,'MOVE')
            path.rename(remote/request.headers['Destination'].split('/')[-1]); return web.Response(status=201)
        app=web.Application(); app.router.add_route('*','/{name}',handle)
        async with TestServer(app) as server:
            self.config.update(url=str(server.make_url('/')),username='backup',allow_http=True)
            for include_temp in (True, False):
                self.config['local_items'] = not include_temp
                payload = SimpleNamespace(json=AsyncMock(return_value={'webdav_items': include_temp}))
                with patch.object(plugin, 'request', payload):
                    self.assertEqual((await self.bot.settings()).status_code, 200)
                for mode in plugin.MODES:
                    self.bot._enqueue(mode)
                await asyncio.gather(*self.bot.tasks.values())
                self.assertTrue(all(r['ok'] for r in self.bot.history), self.bot.history)
                for mode, directory in [('local', self.data/'backups'), ('webdav', remote)]:
                    record = next(r for r in self.bot.history if r['mode'] == mode)
                    with ZipFile(directory / record['name']) as z:
                        expected_temp = include_temp if mode == 'webdav' else not include_temp
                        self.assertEqual('directories/temp/media.jpg' in z.namelist(), expected_temp)
                        self.assertEqual('temp' in json.loads(z.read('manifest.json'))['directories'], expected_temp)
                        self.assertIn('directories/skills/guide.md', z.namelist())
                        self.assertIn('databases/main_db.json', z.namelist())
                        self.assertIn('databases/kb_metadata.json', z.namelist())
        self.assertEqual(len(list(remote.iterdir())), 2)
        self.assertEqual(len(list((self.data/'backups').iterdir())), 2)

    async def test_upload_failure_and_configuration(self):
        with patch.object(plugin,'request',SimpleNamespace(json=AsyncMock(return_value={'local_enabled':True, 'local_cron':'0 4 * * *'}))):
            self.assertEqual((await self.bot.settings()).status_code,200)
        status=json.loads((await self.bot.dashboard()).body)
        self.assertIn('local',status['next']); self.assertNotIn('password',status['settings'])
        with patch.object(plugin,'request',SimpleNamespace(json=AsyncMock(return_value={'local_cron':'?' }))):
            self.assertEqual((await self.bot.settings()).status_code,400)
        self.assertEqual(self.config['local_cron'],'0 4 * * *')
        app=web.Application()
        async def fail(request): return web.Response(status=403)
        app.router.add_route('*','/{name}',fail)
        async with TestServer(app) as server:
            self.config.update(url=str(server.make_url('/')),allow_http=True)
            await self.bot._backup('webdav')
        self.assertFalse(self.bot.history[0]['ok']);self.assertIn('403',self.bot.history[0]['error'])
        self.assertEqual(list((self.data/'backups').iterdir()),[])
