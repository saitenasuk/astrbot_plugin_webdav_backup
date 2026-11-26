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
                path.write_bytes(await request.read()); return web.Response(status=201)
            if request.method == 'PROPFIND':
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
            with patch.object(
                plugin, 'request', SimpleNamespace(json=AsyncMock(return_value=payload))
            ):
                self.assertEqual((await self.bot.test_connection()).status_code, 200)
                self.assertEqual(auth[-1], 'Basic dGVzdDpzYXZlZA==')

    async def test_local_retention_and_storage_metrics(self):
        backups = self.data / 'backups'
        backups.mkdir(exist_ok=True)
        other = backups / 'native_manual.zip'
        old_one = backups / 'astrbot_backup_20250101_000000_local_000000000001.zip'
        old_two = backups / 'astrbot_backup_20250102_000000_local_000000000002.zip'
        for path in (other, old_one, old_two):
            path.write_bytes(b'zip-data')

        for bad in (-1, 1001, True, '2'):
            with self.subTest(bad=bad), patch.object(
                plugin,
                'request',
                SimpleNamespace(json=AsyncMock(return_value={'local_keep_count': bad})),
            ):
                self.assertEqual((await self.bot.settings()).status_code, 400)

        with patch.object(
            plugin,
            'request',
            SimpleNamespace(json=AsyncMock(return_value={'local_keep_count': 2})),
        ):
            self.assertEqual((await self.bot.settings()).status_code, 200)

        await self.bot._backup('local', trigger='schedule')
        record = self.bot.history[0]
        self.assertTrue(record['ok'], record)
        self.assertEqual(record['trigger'], 'schedule')
        self.assertEqual(record['pruned'], 1)
        self.assertGreaterEqual(record['duration_ms'], 0)
        self.assertTrue(other.exists())
        self.assertFalse(old_one.exists())
        self.assertTrue(old_two.exists())
        self.assertTrue((backups / record['name']).exists())

        status = json.loads((await self.bot.dashboard()).body)
        self.assertEqual(status['local_storage']['total_count'], 3)
        self.assertEqual(status['local_storage']['plugin_count'], 2)

    async def test_webdav_retention_prunes_plugin_files_and_handles_delete_error(self):
        prefix = '/mount/dav/%E7%A9%BA%20%E6%A0%BC/'
        files = {
            'keep_other.zip': b'keep',
            'astrbot_backup_20250101_000000_webdav_000000000001.zip': b'old1',
            'astrbot_backup_20250102_000000_webdav_000000000002.zip': b'old2',
        }
        fail_delete = False

        async def handle(request):
            name = request.match_info['name']
            if request.method == 'PUT':
                files[name] = await request.read()
                return web.Response(status=201)
            if request.method == 'PROPFIND':
                if name:
                    return properties(request.raw_path, len(files[name]))
                entries = [
                    f'<D:response><D:href>{prefix}</D:href>'
                    '<D:propstat><D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop>'
                    '<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>',
                    f'<D:response><D:href>{prefix}subdir/</D:href>'
                    '<D:propstat><D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop>'
                    '<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>',
                ]
                for fname, content in files.items():
                    entries.append(
                        f'<D:response><D:href>{prefix}{escape(fname)}</D:href>'
                        f'<D:propstat><D:prop><D:getcontentlength>{len(content)}</D:getcontentlength></D:prop>'
                        '<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>'
                    )
                return web.Response(
                    status=207,
                    content_type='application/xml',
                    text=f'<D:multistatus xmlns:D="DAV:">{"".join(entries)}</D:multistatus>',
                )
            if request.method == 'DELETE':
                if fail_delete:
                    return web.Response(status=403)
                del files[name]
                return web.Response(status=204)
            return web.Response(status=405)

        app = web.Application()
        app.router.add_route('*', '/mount/dav/空 格/{name:.*}', handle)
        async with TestServer(app) as server:
            self.config.update(
                url=str(server.make_url('/mount/dav/')),
                remote_dir='/空 格',
                username='backup',
                password='secret',
                allow_http=True,
                webdav_keep_count=2,
            )
            await self.bot._backup('webdav')
            first = self.bot.history[0]
            self.assertTrue(first['ok'], first)
            self.assertEqual(first['pruned'], 1)
            self.assertIn('keep_other.zip', files)
            self.assertNotIn(
                'astrbot_backup_20250101_000000_webdav_000000000001.zip', files
            )
            self.assertIn(
                'astrbot_backup_20250102_000000_webdav_000000000002.zip', files
            )
            self.assertIn(first['name'], files)

            fail_delete = True
            await self.bot._backup('webdav')
            second = self.bot.history[0]
            self.assertTrue(second['ok'], second)
            self.assertIn('403', second['prune_error'])

    async def test_cron_preview(self):
        with patch.object(
            plugin,
            'request',
            SimpleNamespace(
                json=AsyncMock(
                    return_value={
                        'cron': '0 3 * * *',
                        'timezone': 'Asia/Shanghai',
                        'count': 3,
                    }
                )
            ),
        ):
            res = await self.bot.cron_preview()
        self.assertEqual(res.status_code, 200)
        runs = json.loads(res.body)['next_runs']
        self.assertEqual(len(runs), 3)
        self.assertEqual(runs, sorted(runs))

        for bad in (
            {'cron': 'invalid', 'timezone': 'Asia/Shanghai'},
            {'cron': '0 3 * * *', 'timezone': 'Invalid/Zone'},
            {'cron': '0 3 * * *', 'timezone': 'Asia/Shanghai', 'count': 99},
        ):
            with self.subTest(bad=bad), patch.object(
                plugin, 'request', SimpleNamespace(json=AsyncMock(return_value=bad))
            ):
                self.assertEqual((await self.bot.cron_preview()).status_code, 400)

    async def test_local_archive_list_download_and_delete(self):
        backups = self.data / 'backups'
        backups.mkdir(exist_ok=True)
        owned = backups / 'astrbot_backup_20260101_020000_local_abcdef123456.zip'
        other = backups / 'manual_export.zip'
        with ZipFile(owned, 'w') as zf:
            zf.writestr('manifest.json', '{}')
        with ZipFile(other, 'w') as zf:
            zf.writestr('manifest.json', '{}')

        listed = json.loads((await self.bot.list_local_archives()).body)
        by_name = {item['name']: item for item in listed['items']}
        self.assertEqual(set(by_name), {owned.name, other.name})
        self.assertTrue(by_name[owned.name]['owned'])
        self.assertFalse(by_name[other.name]['owned'])

        # Download valid and invalid filenames
        with patch.object(
            plugin, 'request', SimpleNamespace(query={'name': owned.name})
        ):
            res = await self.bot.download_local_archive()
            self.assertEqual(res.status_code, 200)
        for bad in ('', '../secret.zip', 'sub/a.zip', 'notzip.txt', 'missing.zip'):
            with self.subTest(bad=bad), patch.object(
                plugin, 'request', SimpleNamespace(query={'name': bad})
            ):
                res = await self.bot.download_local_archive()
                self.assertIn(res.status_code, (400, 404))

        # Delete valid and invalid filenames
        with patch.object(
            plugin,
            'request',
            SimpleNamespace(json=AsyncMock(return_value={'name': other.name})),
        ):
            res = await self.bot.delete_local_archive()
            self.assertEqual(res.status_code, 200)
        self.assertFalse(other.exists())
        self.assertTrue(owned.exists())

        with patch.object(
            plugin,
            'request',
            SimpleNamespace(json=AsyncMock(return_value={'name': '../owned.zip'})),
        ):
            self.assertEqual((await self.bot.delete_local_archive()).status_code, 400)

    async def test_webdav_archive_list_pull_and_delete(self):
        prefix = '/mount/dav/backups/'
        valid_buf = self.data / 'sample_valid.zip'
        with ZipFile(valid_buf, 'w') as zf:
            zf.writestr('manifest.json', '{"ok": true}')
        remote_files = {
            'astrbot_backup_20260101_030000_webdav_abcdef123456.zip': valid_buf.read_bytes(),
            'corrupt.zip': b'<html>not a zip</html>',
        }

        async def handle(request):
            name = request.match_info['name']
            if request.method == 'PROPFIND':
                entries = [
                    f'<D:response><D:href>{prefix}</D:href>'
                    '<D:propstat><D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop>'
                    '<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>'
                ]
                for fname, content in remote_files.items():
                    entries.append(
                        f'<D:response><D:href>{prefix}{escape(fname)}</D:href>'
                        f'<D:propstat><D:prop><D:getcontentlength>{len(content)}</D:getcontentlength>'
                        '<D:getlastmodified>Thu, 01 Jan 2026 03:00:00 GMT</D:getlastmodified></D:prop>'
                        '<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>'
                    )
                return web.Response(
                    status=207,
                    content_type='application/xml',
                    text=f'<D:multistatus xmlns:D="DAV:">{"".join(entries)}</D:multistatus>',
                )
            if request.method == 'GET':
                if name not in remote_files:
                    return web.Response(status=404)
                return web.Response(body=remote_files[name])
            if request.method == 'DELETE':
                remote_files.pop(name, None)
                return web.Response(status=204)
            return web.Response(status=405)

        app = web.Application()
        app.router.add_route('*', '/mount/dav/backups/{name:.*}', handle)
        async with TestServer(app) as server:
            self.config.update(
                url=str(server.make_url('/mount/dav/')),
                remote_dir='/backups',
                username='backup',
                password='secret',
                allow_http=True,
            )
            listed = json.loads((await self.bot.list_webdav_archives()).body)
            by_name = {item['name']: item for item in listed['items']}
            target_name = 'astrbot_backup_20260101_030000_webdav_abcdef123456.zip'
            self.assertIn(target_name, by_name)
            self.assertTrue(by_name[target_name]['owned'])
            self.assertFalse(by_name[target_name]['exists_local'])

            # Pull valid ZIP into data/backups
            with patch.object(
                plugin,
                'request',
                SimpleNamespace(json=AsyncMock(return_value={'name': target_name})),
            ):
                res = await self.bot.pull_webdav_archive()
            self.assertEqual(res.status_code, 200)
            self.assertTrue((self.data / 'backups' / target_name).is_file())

            # Duplicate pull returns 409
            with patch.object(
                plugin,
                'request',
                SimpleNamespace(json=AsyncMock(return_value={'name': target_name})),
            ):
                self.assertEqual((await self.bot.pull_webdav_archive()).status_code, 409)

            # Corrupt remote ZIP is rejected and not written to data/backups
            with patch.object(
                plugin,
                'request',
                SimpleNamespace(json=AsyncMock(return_value={'name': 'corrupt.zip'})),
            ):
                self.assertEqual((await self.bot.pull_webdav_archive()).status_code, 502)
            self.assertFalse((self.data / 'backups' / 'corrupt.zip').exists())

            # Delete remote ZIP
            with patch.object(
                plugin,
                'request',
                SimpleNamespace(json=AsyncMock(return_value={'name': 'corrupt.zip'})),
            ):
                self.assertEqual((await self.bot.delete_webdav_archive()).status_code, 200)
            self.assertNotIn('corrupt.zip', remote_files)

    async def test_archive_and_webdav_progress_callbacks(self):
        archive_events = []
        target = self.data / 'progress_test.zip'
        await plugin.create_archive(
            self.data,
            target,
            self.db,
            self.kb_manager,
            include_temp=False,
            plugin_folder=plugin.NAME,
            on_progress=archive_events.append,
        )
        stages = [e['stage'] for e in archive_events]
        for expected in ('database', 'kb', 'config', 'attachments', 'directories', 'manifest'):
            self.assertIn(expected, stages)

        upload_events = []
        seen_content_length = None

        async def handle(request):
            nonlocal seen_content_length
            if request.method == 'PUT':
                seen_content_length = request.headers.get('Content-Length')
                body = await request.read()
                self.assertEqual(len(body), target.stat().st_size)
                return web.Response(status=201)
            if request.method == 'PROPFIND':
                return properties(request.raw_path, target.stat().st_size)
            return web.Response(status=405)

        app = web.Application()
        app.router.add_route('*', '/dav/{name}', handle)
        async with TestServer(app) as server:
            await plugin.upload(
                target,
                str(server.make_url('/dav/')),
                'u',
                'p',
                on_progress=upload_events.append,
            )
        self.assertEqual(seen_content_length, str(target.stat().st_size))
        self.assertTrue(
            any(
                e['stage'] == 'upload'
                and e['current'] == target.stat().st_size
                and e['percent'] == 100.0
                for e in upload_events
            ),
            upload_events,
        )
        self.assertEqual(upload_events[-1]['stage'], 'verify')

    async def test_sse_events_stream_snapshot_progress_and_completion(self):
        response = await self.bot.events()
        self.assertEqual(response.media_type, 'text/event-stream')
        self.assertEqual(response.headers.get('x-accel-buffering'), 'no')

        stream = response.body_iterator
        first = await anext(stream)
        self.assertIn('event: snapshot\n', first)

        backup_task = asyncio.create_task(self.bot._backup('local'))
        received = []
        while True:
            chunk = await asyncio.wait_for(anext(stream), timeout=10)
            received.append(chunk)
            if chunk.startswith('event: completed\n'):
                break
        await backup_task
        self.assertTrue(any(c.startswith('event: progress\n') for c in received))
        await stream.aclose()
        self.assertEqual(len(self.bot._subscribers), 0)
