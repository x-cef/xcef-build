import importlib.util
import json
from pathlib import Path
import tempfile
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release', ROOT / 'scripts/release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.rows = release.matrix(json.loads((ROOT / 'config/builds.json').read_text()))
        self.row = self.rows[0]
        self.plan = dict(xcef_version='1.0.0', source_sha='a' * 40, build_sha='b' * 40,
                         source_repository=release.SOURCE_REPO, source_tag='v1.0.0',
                         release_tag='v1.0.0', matrix=[self.row])

    def package(self, version='1.0.0'):
        directory = self.root / 'packages'
        directory.mkdir(exist_ok=True)
        data = dict(xcef_version=version, cef_sdk_version=self.row['cef_version'],
                    platform=self.row['platform'], architecture=self.row['architecture'])
        name = f"xcef-{version}-{self.row['cef_version']}-{self.row['platform']}-{self.row['architecture']}.zip"
        with zipfile.ZipFile(directory / name, 'w') as archive:
            archive.writestr('XCef/xcef-version-windows-x64.json', json.dumps(data))
            archive.writestr('XCef/bin/Win64/example.bin', b'example')
        return directory

    def test_default_matrix_has_four_cef151_targets(self):
        self.assertEqual(len(self.rows), 4)
        self.assertEqual({(r['platform'], r['architecture']) for r in self.rows}, {('windows', 'x64'), ('macos', 'arm64'), ('macos', 'x64'), ('linux', 'x64')})
        self.assertEqual({r['cef_major'] for r in self.rows}, {151})

    def test_duplicate_matrix_rejected(self):
        config = json.loads((ROOT / 'config/builds.json').read_text())
        config['cef_versions'].append(config['cef_versions'][0])
        with self.assertRaises(ValueError):
            release.matrix(config)

    def test_version_mismatch_rejected(self):
        (self.root / 'VERSION').write_text('1.0.0\n')
        with patch.object(release, 'run', return_value='a' * 40):
            with self.assertRaisesRegex(ValueError, 'VERSION'):
                release.verify_source(self.root, 'v1.0.1')
            with self.assertRaisesRegex(ValueError, 'commit'):
                release.verify_source(self.root, 'v1.0.0', 'b' * 40)
            self.assertEqual(release.verify_source(self.root, 'v1.0.0', 'a' * 40), ('1.0.0', 'a' * 40))

    def test_package_preserved_and_manifest_complete(self):
        packages = self.package()
        output = self.root / 'staged'
        release.stage_package(self.plan, self.row, packages, output)
        assets = release.assemble(self.plan, output)
        self.assertEqual(assets[0]['file'], 'xcef-1.0.0-151.3.24+g2384915+chromium-151.0.7922.174-windows-x64.zip')
        self.assertEqual([p.name for p in output.glob('xcef-*.json')],
                         [Path(assets[0]['file']).with_suffix('.json').name])
        with zipfile.ZipFile(packages / assets[0]['file']) as original, zipfile.ZipFile(output / assets[0]['file']) as staged:
            self.assertNotIn('XCef/bin/Win64/.package', original.namelist())
            for entry in original.infolist():
                self.assertEqual(original.read(entry), staged.read(entry.filename))
            self.assertEqual(staged.read('XCef/bin/Win64/.package').decode(), Path(assets[0]['file']).stem)
        manifest = json.loads((output / 'manifest.json').read_text())
        self.assertEqual(manifest['assets'], assets)
        self.assertIn('manifest.json', (output / 'SHA256SUMS').read_text())

    def test_package_record_on_all_runtime_layouts_preserves_symlinks(self):
        layouts = [('windows', 'x64', 'Win64'), ('linux', 'x64', 'Linux'),
                   ('linux', 'arm64', 'Linux'), ('macos', 'arm64', 'Mac/arm64'),
                   ('macos', 'x64', 'Mac/x86_64'), ('macos', 'x64', 'Mac/x64')]
        for platform, architecture, runtime in layouts:
            with self.subTest(runtime=runtime, architecture=architecture):
                package = self.root / f'{platform}-{architecture}-{runtime.replace("/", "-")}.zip'
                base = f'XCef/bin/{runtime}/'
                link = zipfile.ZipInfo(base + 'XCef.framework/Versions/Current')
                link.create_system = 3
                link.external_attr = (stat.S_IFLNK | 0o777) << 16
                with zipfile.ZipFile(package, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                    archive.comment = b'archive comment'
                    archive.writestr(base + 'example.bin', b'unchanged payload')
                    archive.writestr(link, b'A')
                original = package.read_bytes()
                row = dict(platform=platform, architecture=architecture)
                release.add_package_record(package, row)
                with zipfile.ZipFile(package) as archive:
                    self.assertEqual(archive.read(base + '.package'), package.stem.encode())
                    self.assertEqual(archive.read(base + 'example.bin'), b'unchanged payload')
                    self.assertEqual(archive.getinfo(link.filename).external_attr, link.external_attr)
                    self.assertEqual(archive.read(link.filename), b'A')
                    self.assertEqual(archive.comment, b'archive comment')
                    marker_offset = archive.getinfo(base + '.package').header_offset
                    self.assertEqual(package.read_bytes()[:marker_offset], original[:marker_offset])
                release.add_package_record(package, row)
                with zipfile.ZipFile(package) as archive:
                    self.assertEqual(archive.namelist().count(base + '.package'), 1)

    def test_existing_wrong_or_symlink_record_is_rejected(self):
        for symlink in (False, True):
            package = self.root / 'identity.zip'
            entry = zipfile.ZipInfo('XCef/bin/Win64/.package')
            entry.create_system = 3
            entry.external_attr = ((stat.S_IFLNK if symlink else stat.S_IFREG) | 0o644) << 16
            with zipfile.ZipFile(package, 'w') as archive:
                archive.writestr(entry, b'identity' if symlink else b'wrong')
            with self.assertRaisesRegex(ValueError, '.package'):
                release.add_package_record(package, dict(platform='windows', architecture='x64'))

    def test_wrong_embedded_version_rejected(self):
        with self.assertRaisesRegex(ValueError, 'version/target'):
            release.stage_package(self.plan, self.row, self.package('2.0.0'), self.root / 'staged')

    def test_abbreviated_filename_rejected(self):
        packages = self.package()
        next(packages.glob('*.zip')).rename(packages / 'XCef-1.0.0-cef151-windows-x64.zip')
        with self.assertRaisesRegex(ValueError, 'filename'):
            release.stage_package(self.plan, self.row, packages, self.root / 'staged')

    def test_corrupt_package_rejected(self):
        output = self.root / 'staged'
        release.stage_package(self.plan, self.row, self.package(), output)
        next(output.glob('*.zip')).write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            release.assemble(self.plan, output)

    def test_missing_matrix_result_rejected(self):
        output = self.root / 'staged'
        release.stage_package(self.plan, self.row, self.package(), output)
        self.plan['matrix'] = self.rows
        with self.assertRaises(FileNotFoundError):
            release.assemble(self.plan, output)

    def test_extra_package_rejected(self):
        output = self.root / 'staged'
        release.stage_package(self.plan, self.row, self.package(), output)
        (output / 'unexpected.zip').write_bytes(b'extra')
        with self.assertRaisesRegex(ValueError, 'Unexpected'):
            release.assemble(self.plan, output)

    def published_fixture(self):
        output = self.root / 'staged'
        release.stage_package(self.plan, self.row, self.package(), output)
        assets = release.assemble(self.plan, output)
        previous = dict(self.plan, assets=assets)
        remote = [dict(id=i, name=name, size=size, updated_at='before') for i, (name, size) in
                  enumerate([(assets[0]['file'], assets[0]['size']), ('manifest.json', 100)])]
        return output, assets, previous, remote

    def remote_command(self, previous, remote):
        def command(*args):
            if args[1:3] == ('release', 'download'):
                Path(args[-1], 'manifest.json').write_text(json.dumps(previous))
            elif args[1] == 'api':
                return json.dumps([remote])
            else:
                self.fail(f'Unexpected mutation: {args}')
        return command

    def package_name(self, row):
        return f"cef_binary_{row['cef_version']}_{row['cef_platform']}.tar.bz2"

    def test_package_selection_supports_multiple_versions_and_targets(self):
        config = json.loads((ROOT / 'config/builds.json').read_text())
        selected = [self.rows[0], self.rows[1], self.rows[-1]]
        packages = [self.package_name(row) for row in selected]
        rows = release.requested_matrix(config, dict(cef_packages=json.dumps(packages)))
        self.assertEqual([row['cef_package'] for row in rows], packages)
        self.assertEqual([row['cef_version'] for row in rows], [row['cef_version'] for row in selected])
        extra = packages[0].replace(selected[0]['cef_version'], '151.3.25+gabc+chromium-151.0.7922.175')
        rows = release.requested_matrix(config, dict(cef_packages=[packages[0], extra]))
        self.assertEqual(len({row['key'] for row in rows}), 2)
        self.assertEqual(release.requested_matrix(config, {}), self.rows)

    def test_invalid_or_duplicate_packages_rejected(self):
        config = json.loads((ROOT / 'config/builds.json').read_text())
        package = self.package_name(self.row)
        for packages in ([package, package], [package.replace('.tar.bz2', '_beta.tar.bz2')],
                         ['../' + package], ['not-a-package'], [None], 'invalid-json',
                         [package.replace(self.row['cef_platform'], 'unsupported')],
                         [package.replace(self.row['cef_platform'], 'linuxarm64')],
                         [package.replace('chromium-', 'chromium-999.')]):
            with self.subTest(packages=packages), self.assertRaises(ValueError):
                release.requested_matrix(config, dict(cef_packages=packages))
        with patch.dict('os.environ', GITHUB_EVENT_NAME='workflow_dispatch'):
            for packages in ([], '[]'):
                with self.assertRaises(ValueError):
                    release.requested_matrix(config, dict(cef_packages=packages))

    def test_existing_assets_allowed_and_snapshot_detects_replacements(self):
        _, _, previous, remote = self.published_fixture()
        with patch.dict('os.environ', GITHUB_REPOSITORY='x-cef/xcef-build'), \
             patch.object(release, 'run', side_effect=self.remote_command(previous, remote)):
            self.assertEqual(release.existing_state(dict(id=1, draft=False), self.plan)[0], previous)
            first = release.existing_state(dict(id=1, draft=False), self.plan)[1]
            self.assertNotEqual(release.existing_state(dict(id=1, draft=True), self.plan)[1], first)
            remote[0]['id'] = 99
            self.assertNotEqual(release.existing_state(dict(id=1, draft=False), self.plan)[1], first)

    def test_update_rejects_missing_or_immutable(self):
        for info in (None, dict(draft=False, immutable=True)):
            with self.subTest(info=info), patch.object(release, 'run') as command:
                with self.assertRaises(ValueError):
                    release.existing_state(info, self.plan)
                command.assert_not_called()

    def test_update_allows_changed_source_but_rejects_missing_asset(self):
        _, _, previous, remote = self.published_fixture()
        plan = dict(self.plan)
        with patch.dict('os.environ', GITHUB_REPOSITORY='x-cef/xcef-build'), \
             patch.object(release, 'run', side_effect=self.remote_command(previous, remote)):
            previous['source_sha'] = 'c' * 40
            previous['assets'][0]['source_sha'] = 'c' * 40
            self.assertEqual(release.existing_state(dict(id=1, draft=False), plan)[0], previous)
            previous['source_sha'] = plan['source_sha']
            remote.pop(0)
            with self.assertRaisesRegex(ValueError, 'do not match manifest'):
                release.existing_state(dict(id=1, draft=False), plan)

    def test_append_merges_catalog_and_preserves_old_provenance(self):
        output, assets, previous, _ = self.published_fixture()
        old_row = self.rows[1]
        old = dict(assets[0], file=release.sdk_filename(self.plan, old_row),
                   platform=old_row['platform'], architecture=old_row['architecture'], build_sha='c' * 40, source_sha='d' * 40)
        previous = dict(previous, assets=[old], matrix=[old_row])
        plan = dict(self.plan, base_fingerprint='snapshot')
        with patch.object(release, 'existing_state', return_value=(previous, 'snapshot')), \
             patch.object(release, 'run') as command:
            release.finalize_existing({'draft': False}, plan, assets, output)
        catalog = json.loads((output / 'manifest.json').read_text())
        self.assertEqual({a['file']: a for a in catalog['assets']}, {old['file']: old, assets[0]['file']: assets[0]})
        self.assertEqual(len(catalog['matrix']), 2)
        self.assertNotIn('base_fingerprint', catalog)
        self.assertEqual(json.loads((output / 'build-plan.json').read_text())['matrix'], catalog['matrix'])
        self.assertIn(f"{old['sha256']}  {old['file']}", (output / 'SHA256SUMS').read_text())
        calls = [c.args for c in command.call_args_list]
        self.assertEqual(len(calls), 2)
        self.assertIn('--clobber', calls[0])
        self.assertNotIn(str(output / old['file']), calls[0])
        self.assertIn('--clobber', calls[1])

    def test_overwrite_replaces_metadata_without_duplicate(self):
        output, assets, previous, _ = self.published_fixture()
        previous['assets'] = [dict(assets[0], sha256='old', build_sha='c' * 40, source_sha='d' * 40)]
        plan = dict(self.plan, base_fingerprint='snapshot')
        with patch.object(release, 'existing_state', return_value=(previous, 'snapshot')), \
             patch.object(release, 'run') as command:
            release.finalize_existing({'draft': False}, plan, assets, output)
        self.assertEqual(json.loads((output / 'manifest.json').read_text())['assets'], assets)
        self.assertIn('--clobber', command.call_args_list[0].args)

    def test_publish_merges_latest_state_after_another_package(self):
        output, assets, previous, _ = self.published_fixture()
        plan = dict(self.plan, base_fingerprint='before')
        with patch.object(release, 'existing_state', return_value=(previous, 'after')), \
             patch.object(release, 'run') as command:
            release.finalize_existing({'draft': False}, plan, assets, output)
            self.assertEqual(command.call_count, 2)

    def test_failed_package_upload_does_not_upload_catalog(self):
        output, assets, previous, _ = self.published_fixture()
        plan = dict(self.plan, base_fingerprint='snapshot')
        with patch.object(release, 'existing_state', return_value=(previous, 'snapshot')), \
             patch.object(release, 'run', side_effect=OSError('upload failed')) as command:
            with self.assertRaises(OSError):
                release.finalize_existing({'draft': False}, plan, assets, output)
            self.assertEqual(command.call_count, 1)

    def test_new_release_is_published_with_all_verified_assets(self):
        output, assets, _, _ = self.published_fixture()
        with patch.dict('os.environ', BUILD_PLAN=json.dumps(self.plan)), \
             patch.object(release, 'release_info', return_value=None), \
             patch.object(release, 'run') as command:
            release.finalize(SimpleNamespace(directory=output))
        args = command.call_args.args
        self.assertEqual(command.call_count, 1)
        self.assertEqual(args[:4], ('gh', 'release', 'create', 'v1.0.0'))
        self.assertNotIn('--draft', args)
        for name in (assets[0]['file'], 'build-plan.json', 'manifest.json', 'SHA256SUMS'):
            self.assertIn(str(output / name), args)
        self.assertEqual(json.loads((output / 'build-plan.json').read_text()), self.plan)

    def test_single_package_publish_does_not_wait_for_full_matrix(self):
        output, assets, _, _ = self.published_fixture()
        plan = dict(self.plan, matrix=self.rows)
        with patch.dict('os.environ', BUILD_PLAN=json.dumps(plan), BUILD_TARGET=json.dumps(self.row)), \
             patch.object(release, 'release_info', return_value=None), \
             patch.object(release, 'run') as command:
            release.finalize(SimpleNamespace(directory=output))
        self.assertEqual(json.loads((output / 'manifest.json').read_text())['matrix'], [self.row])
        self.assertEqual(command.call_count, 1)
        invalid = dict(self.row, cef_version='invalid')
        with patch.dict('os.environ', BUILD_PLAN=json.dumps(plan), BUILD_TARGET=json.dumps(invalid)), \
             self.assertRaisesRegex(ValueError, 'not in the build plan'):
            release.finalize(SimpleNamespace(directory=output))

    def test_new_release_is_not_created_after_failed_validation(self):
        output, _, _, _ = self.published_fixture()
        next(output.glob('*.zip')).write_bytes(b'corrupt')
        with patch.dict('os.environ', BUILD_PLAN=json.dumps(self.plan)), \
             patch.object(release, 'run') as command:
            with self.assertRaisesRegex(ValueError, 'checksum'):
                release.finalize(SimpleNamespace(directory=output))
        command.assert_not_called()

    def test_release_created_by_previous_package_is_reused(self):
        output, _, _, _ = self.published_fixture()
        with patch.dict('os.environ', BUILD_PLAN=json.dumps(self.plan)), \
             patch.object(release, 'release_info', return_value={'draft': False}), \
             patch.object(release, 'finalize_existing') as update:
            release.finalize(SimpleNamespace(directory=output))
        update.assert_called_once()

    def test_existing_draft_publishes_only_after_catalog_upload(self):
        output, assets, previous, _ = self.published_fixture()
        plan = dict(self.plan, base_fingerprint='snapshot')
        with patch.object(release, 'existing_state', return_value=(previous, 'snapshot')), \
             patch.object(release, 'run') as command:
            release.finalize_existing({'draft': True}, plan, assets, output)
        self.assertEqual(command.call_args_list[-1].args, ('gh', 'release', 'edit', 'v1.0.0', '--draft=false'))
        self.assertEqual(command.call_count, 3)
        with patch.object(release, 'existing_state', return_value=(previous, 'snapshot')), \
             patch.object(release, 'run', side_effect=['', OSError('catalog upload failed')]) as command:
            with self.assertRaises(OSError):
                release.finalize_existing({'draft': True}, plan, assets, output)
        self.assertEqual(command.call_count, 2)

    def test_prepare_update_is_read_only_and_filters_builds(self):
        event = self.root / 'event.json'
        event.write_text(json.dumps(dict(inputs=dict(source_tag='v1.0.0',
                             cef_packages=json.dumps([self.package_name(self.row)])))))
        output = self.root / 'output'
        index = {self.row['cef_platform']: dict(versions=[dict(cef_version=self.row['cef_version'],
                                                          channel='stable', files=[dict(type='standard', name=self.package_name(self.row))])])}
        index_file = self.root / 'index.json'
        index_file.write_text(json.dumps(index))
        with patch.dict('os.environ', GITHUB_EVENT_PATH=str(event), GITHUB_EVENT_NAME='workflow_dispatch',
                        GITHUB_OUTPUT=str(output), GITHUB_SHA=self.plan['build_sha']), \
             patch.object(release, 'verify_source', return_value=('1.0.0', self.plan['source_sha'])), \
             patch.object(release, 'release_info', return_value=dict(id=1, draft=False)), \
             patch.object(release, 'existing_state', return_value=({}, 'snapshot')), \
             patch.object(release.urllib.request, 'urlopen', return_value=index_file.open('rb')), \
             patch.object(release, 'write_json'), patch.object(release, 'run') as command:
            release.prepare(SimpleNamespace(source='source'))
            with patch.object(release, 'release_info', return_value=None), \
                 patch.object(release.urllib.request, 'urlopen', return_value=index_file.open('rb')):
                release.prepare(SimpleNamespace(source='source'))
            entry = index[self.row['cef_platform']]['versions'][0]
            for change in ('beta', 'wrong-file'):
                entry['channel'] = 'beta' if change == 'beta' else 'stable'
                entry['files'][0]['name'] = self.package_name(self.row) if change == 'beta' else 'other.tar.bz2'
                index_file.write_text(json.dumps(index))
                with patch.object(release.urllib.request, 'urlopen', return_value=index_file.open('rb')):
                    with self.assertRaisesRegex(ValueError, 'Stable standard SDK unavailable'):
                        release.prepare(SimpleNamespace(source='source'))
        command.assert_not_called()
        plan = json.loads(next(line[5:] for line in output.read_text().splitlines() if line.startswith('plan=')))
        self.assertEqual(plan['matrix'][0]['cef_package'], self.package_name(self.row))
        self.assertNotIn('base_fingerprint', plan)
        self.assertNotIn('release_mode', plan)


if __name__ == '__main__':
    unittest.main()
