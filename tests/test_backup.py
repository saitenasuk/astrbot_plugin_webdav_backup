import asyncio
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from xml.sax.saxutils import escape
from zipfile import ZipFile

from aiohttp import web
from aiohttp.test_utils import TestServer
from astrbot.core.backup.importer import AstrBotImporter
from astrbot.core.db.sqlite import SQLiteDatabase
from astrbot.core.knowledge_base.kb_db_sqlite import KBSQLiteDatabase

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
plugin = importlib.import_module(f'{ROOT.name}.main')


def properties(href, size, status="200 OK"):
    """Build a single-resource DAV multistatus response for the test server.

    Args:
        href: Resource identifier as reported by the server.
        size: Reported file size in bytes.
        status: HTTP status of the size property.

    Returns:
        HTTP 207 response containing the requested DAV property.
    """
    return web.Response(status=207, content_type="application/xml", text=(
        f'<D:multistatus xmlns:D="DAV:"><D:response><D:href>{escape(href)}</D:href>'
        f'<D:propstat><D:prop><D:getcontentlength>{size}</D:getcontentlength></D:prop>'
        f'<D:status>HTTP/1.1 {status}</D:status></D:propstat></D:response></D:multistatus>'
    ))


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

    def test_service_and_remote_directory(self):
        service = 'https://example.org/mount/dav/'
        self.assertEqual(plugin.collection_url(service, False), service)
        self.assertEqual(plugin.collection_url(service, False, '/'), service)
        self.assertEqual(plugin.collection_url(service, False, '/my backups/知识/'),
                         service + 'my%20backups/%E7%9F%A5%E8%AF%86/')
        for outside in ('/../elsewhere', '//other.example', 'https://other.example', '/%2e%2e', '/a%2fb'):
            with self.subTest(outside=outside), self.assertRaises(ValueError):
                plugin.collection_url(service, False, outside)

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

    async def test_webdav_direct_put_and_independent_selection(self):
        remote=self.data/'remote'
        async def handle(request):
            self.assertIsNotNone(request.headers.get('Authorization'))
            path=remote/request.match_info['name']
            if request.method=='PUT':
                self.assertEqual(self.bot.phases['webdav'], '正在上传')
                path.write_bytes(await request.read()); return web.Response(status=201)
            if request.method == 'PROPFIND':
                self.assertEqual(self.bot.phases['webdav'], '正在校验')
                self.assertEqual(request.headers['Depth'], '0')
                return properties(request.path, path.stat().st_size)
            self.fail(f'Unexpected WebDAV method: {request.method}')
        app=web.Application(); app.router.add_route('*','/mount/dav/backups/{name}',handle)
        async with TestServer(app) as server:
            self.config.update(url=str(server.make_url('/mount/dav/')), username='backup', allow_http=True)
            for include_temp in (True, False):
                self.config['local_items'] = not include_temp
                payload = SimpleNamespace(json=AsyncMock(return_value={'webdav_items': include_temp, 'remote_dir': '/backups'}))
                with patch.object(plugin, 'request', payload):
                    self.assertEqual((await self.bot.settings()).status_code, 200)
                for mode in plugin.MODES:
                    self.bot._enqueue(mode)
                await asyncio.gather(*self.bot.tasks.values())
                self.assertTrue(all(r['ok'] for r in self.bot.history), self.bot.history)
                for mode, directory in [('local', self.data/'backups'), ('webdav', remote)]:
                    record = next(r for r in self.bot.history if r['mode'] == mode)
                    self.assertEqual(list(record['stages']), ['准备', '打包'] + (['上传', '校验'] if mode == 'webdav' else ['保存']))
                    self.assertGreaterEqual(record['duration'], 0)
                    self.assertAlmostEqual(sum(record['stages'].values()), record['duration'], delta=0.01)
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
        self.assertEqual(self.bot.history[0]['failed_phase'], '上传')
        self.assertNotIn('校验', self.bot.history[0]['stages'])
        self.assertEqual(list((self.data/'backups').iterdir()),[])


    async def test_false_webdav_success_is_rejected(self):
        async def handle(request):
            if request.method == 'PUT':
                nonlocal size
                size = len(await request.read())
                return web.Response(status=201)
            self.assertEqual(request.method, 'PROPFIND')
            if request.path == '/':
                self.assertIn(fault, ('wrong_resource', 'prop_status'))
                self.assertEqual(request.headers['Depth'], '1')
                return properties('/different.zip', size)
            if fault == 'missing':
                return web.Response(status=404)
            if fault == 'html':
                return web.Response(text='<html>Login</html>')
            if fault == 'bad_xml':
                return web.Response(status=207, text='<html>')
            if fault == 'wrong_resource':
                return properties('/different.zip', size)
            if fault == 'prop_status':
                return properties(request.path, size, '404 Not Found')
            return properties(request.path, size - 1)

        size = None
        app = web.Application()
        app.router.add_route('*', '/{name:.*}', handle)
        async with TestServer(app) as server:
            self.config.update(url=str(server.make_url('/')), allow_http=True)
            for fault, error in [('missing', 'HTTP 404'), ('html', 'HTTP 200'),
                                 ('bad_xml', '文件属性'), ('wrong_resource', '未确认'),
                                 ('prop_status', '未确认'), ('truncated', '大小不符')]:
                with self.subTest(fault=fault):
                    await self.bot._backup('webdav')
                    record = self.bot.history[0]
                    self.assertFalse(record['ok'], record)
                    self.assertIn(error, record['error'])
                    self.assertEqual(record['failed_phase'], '校验')
                    self.assertTrue(record['remote'].endswith('.zip'))
                    self.assertEqual(json.loads(self.bot.history_file.read_text())[0], record)

    async def test_directory_only_webdav_verification(self):
        methods = []
        files = {}
        prefix = '/mount/dav/'

        async def handle(request):
            name = request.match_info['name']
            methods.append((request.method, request.headers.get('Depth')))
            if request.method == 'PUT':
                files[name] = await request.read()
            elif request.method == 'DELETE':
                del files[name]
            elif name:
                self.assertEqual(request.headers['Depth'], '0')
                return web.Response(status=207, text=(
                    f'<multistatus xmlns="DAV:"><response><href>{request.path}/</href>'
                    '<propstat><prop><resourcetype><collection/></resourcetype></prop>'
                    '<status>HTTP/1.1 200 OK</status></propstat></response></multistatus>'
                ))
            else:
                self.assertEqual(request.headers['Depth'], '1')
                filename, = files
                href = {'relative': filename, 'absolute': prefix + filename,
                        'other_folder': '/other/' + filename, 'other_host': 'https://other.invalid' + prefix + filename,
                        'missing': 'some-other.zip'}.get(case, prefix + filename)
                return properties(href, len(files[filename]) + (case == 'truncated'),
                                  '404 Not Found' if case == 'no_size' else '200 OK')
            return web.Response(status=204)

        app = web.Application()
        app.router.add_route('*', prefix + '{name:.*}', handle)
        async with TestServer(app) as server:
            self.config.update(url=str(server.make_url(prefix)), allow_http=True)
            for case, error in [('relative', None), ('absolute', None), ('truncated', '大小不符'),
                                ('other_folder', '目录或主机'), ('other_host', '目录或主机'),
                                ('missing', '未找到'), ('no_size', 'HTTP 404')]:
                with self.subTest(case=case):
                    methods.clear()
                    payload = {'url': self.config['url'], 'allow_http': True}
                    with patch.object(plugin, 'request', SimpleNamespace(json=AsyncMock(return_value=payload))):
                        result = await self.bot.test_connection()
                    self.assertEqual(methods, [('PUT', None), ('PROPFIND', '0'),
                                               ('PROPFIND', '1'), ('DELETE', None)])
                    self.assertFalse(files)
                    self.assertEqual(result.status_code, 502 if error else 200)
                    if error:
                        self.assertIn(error, json.loads(result.body)['message'])
            case = 'relative'
            await self.bot._backup('webdav')
            record = self.bot.history[0]
            self.assertTrue(record['ok'], record)
            self.assertEqual(list(files), [record['name']])
            self.assertEqual(len(files[record['name']]), record['size'])

    async def test_recheck_reads_only_and_persists_latest_attempt(self):
        """Check a retained receipt without changing the original backup outcome."""
        methods = []

        async def handle(request):
            methods.append((request.method, request.headers['Depth']))
            self.assertEqual(request.headers['Authorization'], 'Basic dXNlcjplbnY=')
            if missing:
                return web.Response(status=404)
            if request.path.endswith('/'):
                return properties('/dav/archive.zip', size)
            return properties('/unrelated.zip', 0)

        app = web.Application()
        app.router.add_route('*', '/dav/{name:.*}', handle)
        async with TestServer(app) as server:
            self.config.update(url=str(server.make_url('/dav/')), allow_http=True, username='user')
            record = dict(mode='webdav', ok=True, time='2026-11-01T03:00:00Z',
                          name='archive.zip', remote=str(server.make_url('/dav/archive.zip')), size=4096)
            self.bot.history = [record]
            request = SimpleNamespace(json=AsyncMock(return_value={'time': record['time']}))
            with patch.object(plugin, 'request', request), patch.dict(plugin.os.environ, ASTRBOT_WEBDAV_PASSWORD='env'):
                for size, missing, success in [(4096, False, True), (1024, False, False), (4096, True, False)]:
                    methods.clear()
                    response = await self.bot.verify_remote()
                    receipt = json.loads(response.body)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(receipt['ok'], success)
                    self.assertTrue(receipt['time'])
                    self.assertEqual(methods, [('PROPFIND', '0')] if missing else [('PROPFIND', '0'), ('PROPFIND', '1')])
                    if not success:
                        self.assertIn('404' if missing else '大小不符', receipt['error'])
                    self.assertTrue(record['ok'])
                    self.assertEqual(json.loads(self.bot.history_file.read_text())[0]['recheck'], receipt)
                methods.clear()
                self.config['remote_dir'] = '/new'
                self.assertEqual((await self.bot.verify_remote()).status_code, 409)
                self.config['remote_dir'] = '/'
                async with self.bot.recheck_lock:
                    self.assertEqual((await self.bot.verify_remote()).status_code, 409)
                for payload, expected in [({'time': 'expired'}, 404), ({'time': record['time'], 'remote': record['remote']}, 400)]:
                    request.json.return_value = payload
                    self.assertEqual((await self.bot.verify_remote()).status_code, expected)
                request.json.return_value = {'time': record['time']}
                record.pop('remote')
                self.assertEqual((await self.bot.verify_remote()).status_code, 400)
                self.assertEqual(methods, [])

    async def test_probe_uses_draft_credentials_and_only_deletes_its_own_files(self):
        methods = []
        auth = []
        files = {'keep.zip': b'existing'}
        async def handle(request):
            name = request.match_info['name']
            methods.append(request.method)
            auth.append(request.headers.get('Authorization'))
            if request.method == 'PUT':
                self.assertEqual(request.headers['If-None-Match'], '*')
                if fault == 'exists':
                    return web.Response(status=412)
                files[name] = await request.read()
            elif request.method == 'PROPFIND':
                if fault == 'verify':
                    return web.Response(status=403)
                return properties(request.raw_path, len(files[name]))
            elif request.method == 'DELETE':
                if fault == 'delete':
                    return web.Response(status=403)
                del files[name]
            return web.Response(status=204)

        app = web.Application()
        app.router.add_route('*', '/draft/{folder}/{name}', handle)
        async with TestServer(app) as server:
            collection = str(server.make_url('/draft/'))
            self.config.update(url=str(server.make_url('/saved/')), username='test', password='saved', allow_http=True)
            previous = dict(self.config)
            for fault, expected in [('ok', ['PUT','PROPFIND','DELETE']), ('exists', ['PUT']),
                                    ('verify', ['PUT','PROPFIND','DELETE']), ('delete', ['PUT','PROPFIND','DELETE'])]:
                methods.clear()
                with self.subTest(fault=fault):
                    payload = dict(url=collection, remote_dir='/空 格', username='test', allow_http=True, password='draft')
                    with patch.object(plugin, 'request', SimpleNamespace(json=AsyncMock(return_value=payload))):
                        result = await self.bot.test_connection()
                    self.assertEqual(methods, expected)
                    self.assertEqual(result.status_code, 200 if fault == 'ok' else 502)
                    if fault != 'delete':
                        self.assertEqual(files, {'keep.zip': b'existing'})
                    else:
                        self.assertIn('DELETE', json.loads(result.body)['message'])
                    self.assertEqual(self.config, previous)
                    self.assertEqual(self.bot.history, [])
            self.assertEqual(set(auth), {'Basic dGVzdDpkcmFmdA=='})
            payload = dict(url='https://elsewhere.invalid/', username='test')
            with patch.object(plugin, 'request', SimpleNamespace(json=AsyncMock(return_value=payload))):
                self.assertEqual((await self.bot.test_connection()).status_code, 400)
            payload.update(url=collection, remote_dir='/空 格', allow_http=True)
            fault = 'ok'
            for env, expected_auth in [('', 'Basic dGVzdDpzYXZlZA=='), ('env', 'Basic dGVzdDplbnY=')]:
                with patch.dict(plugin.os.environ, ASTRBOT_WEBDAV_PASSWORD=env), patch.object(
                    plugin, 'request', SimpleNamespace(json=AsyncMock(return_value=payload))
                ):
                    self.assertEqual((await self.bot.test_connection()).status_code, 200)
                    self.assertEqual(auth[-1], expected_auth)
