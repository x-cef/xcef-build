"""Build verified SDK catalogs and automatically publish or update releases."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

SOURCE_REPO = 'x-cef/XCef'


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def matrix(config):
    rows = []
    seen = set()
    for cef in config['cef_versions']:
        if not re.fullmatch(rf"{cef['major']}\.\d+\.\d+\+g[0-9a-f]+\+chromium-{cef['major']}\.[0-9.]+", cef['version']):
            raise ValueError('Invalid or mismatched CEF version')
        for target in config['targets']:
            key = f"cef{cef['major']}-{target['platform']}-{target['architecture']}"
            if key in seen or not re.fullmatch(r'cef\d+-(windows|linux|macos)-(x64|arm64)', key):
                raise ValueError(f'Invalid/duplicate target: {key}')
            seen.add(key)
            rows.append(dict(target, cef_major=cef['major'], cef_version=cef['version'], key=key))
    if not rows:
        raise ValueError('Empty build matrix')
    return rows


def verify_source(source, tag, expected_sha=''):
    if not re.fullmatch(r'v\d+\.\d+\.\d+', tag):
        raise ValueError('Source tag must be vX.Y.Z')
    sha = run('git', '-C', str(source), 'rev-parse', 'HEAD')
    if expected_sha and sha != expected_sha:
        raise ValueError('Source tag does not resolve to the dispatched commit')
    version = (Path(source) / 'VERSION').read_text(encoding='utf-8').strip()
    if tag != 'v' + version:
        raise ValueError('Source tag does not match VERSION')
    return version, sha


def requested_matrix(config, payload):
    packages = payload.get('cef_packages', [])
    if isinstance(packages, str):
        try:
            packages = json.loads(packages)
        except json.JSONDecodeError as error:
            raise ValueError('CEF packages must be a JSON array') from error
    if not isinstance(packages, list) or len(packages) > 32 or any(not isinstance(p, str) for p in packages):
        raise ValueError('CEF packages must be an array of up to 32 filenames')
    if len(set(packages)) != len(packages):
        raise ValueError('Duplicate CEF packages')
    if not packages:
        if os.environ.get('GITHUB_EVENT_NAME') == 'workflow_dispatch':
            raise ValueError('Manual builds require at least one CEF SDK package')
        return matrix(config)
    rows = []
    for package in packages:
        match = re.fullmatch(r'cef_binary_(\d+\.\d+\.\d+\+g[0-9a-f]+\+chromium-\d+\.[0-9.]+)_([a-z0-9]+)\.tar\.bz2', package)
        if not match:
            raise ValueError('Invalid stable standard CEF SDK package filename')
        version, cef_platform = match.groups()
        targets = [target for target in config['targets'] if target['cef_platform'] == cef_platform]
        if len(targets) != 1:
            raise ValueError('Unsupported CEF package platform')
        row = matrix(dict(cef_versions=[dict(major=int(version.split('.')[0]), version=version)], targets=targets))[0]
        row.update(cef_package=package, key=f"cef{version}-{row['platform']}-{row['architecture']}")
        rows.append(row)
    return rows


def release_info(tag):
    # Pagination includes drafts visible to the repository token. API failures are fatal.
    pages = json.loads(run('gh', 'api', '--paginate', '--slurp', f"repos/{os.environ['GITHUB_REPOSITORY']}/releases?per_page=100"))
    return next((r for page in pages for r in page if r['tag_name'] == tag), None)


def prepare(args):
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    payload = event.get('client_payload', {}) if os.environ['GITHUB_EVENT_NAME'] == 'repository_dispatch' else event.get('inputs', {})
    tag = payload.get('source_tag', '')
    expected_sha = payload.get('source_sha', '')
    if os.environ['GITHUB_EVENT_NAME'] == 'repository_dispatch' and not re.fullmatch(r'[0-9a-f]{40}', expected_sha):
        raise ValueError('Dispatch requires a full source SHA')
    version, sha = verify_source(args.source, tag, expected_sha)
    rows = requested_matrix(json.loads(Path('config/builds.json').read_text()), payload)
    plan = dict(schema_version=2, source_repository=SOURCE_REPO, source_tag=tag,
                source_sha=sha, xcef_version=version,
                release_tag=tag, build_sha=os.environ['GITHUB_SHA'],
                configuration='Release', matrix=rows)
    info = release_info(tag)
    if info:
        existing_state(info, plan)
    # Fail before starting builds if the requested standard SDK is unavailable.
    with urllib.request.urlopen('https://cef-builds.spotifycdn.com/index.json', timeout=90) as response:
        index = json.load(response)
    for row in rows:
        versions = index[row['cef_platform']]['versions']
        if not any(v.get('channel') == 'stable' and v['cef_version'] == row['cef_version'] and any(f['type'] == 'standard' and (not row.get('cef_package') or f['name'] == row['cef_package']) for f in v['files']) for v in versions):
            raise ValueError(f"Stable standard SDK unavailable: {row['key']} {row['cef_version']}")
    write_json('.work/build-plan.json', plan)
    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
        for key, value in dict(source_sha=sha, release_tag=plan['release_tag'], matrix=json.dumps({'include': rows}), plan=json.dumps(plan)).items():
            stream.write(f'{key}={value}\n')


def sdk_filename(plan, row):
    # Match XCef scripts/build_sdk.py:create_archive exactly.
    return (f"xcef-{plan['xcef_version']}-{row['cef_version']}-"
            f"{row['platform']}-{row['architecture']}.zip")


def existing_state(info, plan):
    if not info:
        raise ValueError('Asset update requires an existing release')
    if info.get('immutable'):
        raise ValueError('Cannot update an immutable release')
    with tempfile.TemporaryDirectory() as directory:
        run('gh', 'release', 'download', plan['release_tag'], '--pattern', 'manifest.json', '--dir', directory)
        previous = json.loads((Path(directory) / 'manifest.json').read_text(encoding='utf-8'))
    identity = ('source_repository', 'source_tag', 'xcef_version', 'release_tag')
    if any(previous.get(key) != plan[key] for key in identity):
        raise ValueError('Release version or repository does not match the requested tag')
    pages = json.loads(run('gh', 'api', '--paginate', '--slurp',
                          f"repos/{os.environ['GITHUB_REPOSITORY']}/releases/{info['id']}/assets?per_page=100"))
    remote = {a['name']: a for page in pages for a in page}
    seen = set()
    for asset in previous['assets']:
        name = asset['file']
        if name in seen or name not in remote or remote[name]['size'] != asset['size']:
            raise ValueError('Existing release assets do not match manifest')
        seen.add(name)
    if {sdk_filename(previous, row) for row in previous['matrix']} != seen:
        raise ValueError('Existing release matrix does not match manifest')
    # Include asset IDs and timestamps so replacing a same-size ZIP is detected too.
    snapshot = dict(release_id=info['id'], draft=info['draft'], manifest=previous,
                    assets=sorted((a['id'], a['name'], a['size'], a['updated_at']) for a in remote.values()))
    fingerprint = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    return previous, fingerprint


def write_catalog(plan, assets, directory):
    write_json(directory / 'manifest.json', dict(plan, assets=assets))
    checksums = [f"{a['sha256']}  {a['file']}" for a in assets]
    checksums.append(f"{digest(directory / 'manifest.json')}  manifest.json")
    (directory / 'SHA256SUMS').write_text('\n'.join(checksums) + '\n', encoding='utf-8')


def finalize_existing(info, plan, assets, directory):
    previous, fingerprint = existing_state(info, plan)
    merged = {a['file']: a for a in previous['assets']}
    merged.update({a['file']: a for a in assets})
    rows = {sdk_filename(plan, row): row for row in previous['matrix']}
    rows.update({sdk_filename(plan, row): row for row in plan['matrix']})
    catalog = {k: v for k, v in plan.items() if k != 'base_fingerprint'}
    catalog['matrix'] = [rows[name] for name in sorted(rows)]
    write_catalog(catalog, [merged[name] for name in sorted(merged)], directory)
    # Upload packages first; never advertise new packages before they exist.
    command = ['gh', 'release', 'upload', plan['release_tag'],
               *(str(directory / a['file']) for a in assets), '--clobber']
    run(*command)
    write_json(directory / 'build-plan.json', catalog)
    run('gh', 'release', 'upload', plan['release_tag'], str(directory / 'build-plan.json'),
        str(directory / 'manifest.json'), str(directory / 'SHA256SUMS'), '--clobber')
    if info['draft']:
        run('gh', 'release', 'edit', plan['release_tag'], '--draft=false')


def package_record_path(row, names):
    if row['platform'] == 'windows':
        roots = ['XCef/bin/Win64/']
    elif row['platform'] == 'linux':
        roots = ['XCef/bin/Linux/']
    else:
        architectures = ['x86_64', 'x64'] if row['architecture'] == 'x64' else ['arm64']
        roots = [f'XCef/bin/Mac/{architecture}/' for architecture in architectures]
    matches = [root for root in roots if any(name.startswith(root) for name in names)]
    if len(matches) != 1:
        raise ValueError('SDK runtime directory is missing or ambiguous')
    return matches[0] + '.package'


def check_package_record(archive, path, expected):
    entries = [entry for entry in archive.infolist() if entry.filename == path]
    if len(entries) != 1 or stat.S_ISLNK(entries[0].external_attr >> 16):
        raise ValueError('SDK .package must be a single regular file')
    if archive.read(entries[0]) != expected.encode('utf-8'):
        raise ValueError('SDK .package identity does not match package filename')


def add_package_record(package, row):
    # Append only the marker: existing compressed entries and symlink metadata stay intact.
    with zipfile.ZipFile(package, 'a') as archive:
        path = package_record_path(row, archive.namelist())
        if path not in archive.namelist():
            entry = zipfile.ZipInfo(path)
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, package.stem.encode('utf-8'))
        check_package_record(archive, path, package.stem)


def stage_package(plan, row, package_dir, output):
    packages = list(Path(package_dir).glob('*.zip'))
    if len(packages) != 1:
        raise ValueError('Expected exactly one SDK ZIP')
    package = packages[0]
    with zipfile.ZipFile(package) as archive:
        manifest = json.loads(archive.read(f"XCef/xcef-version-{row['platform']}-{row['architecture']}.json"))
    expected = dict(xcef_version=plan['xcef_version'], cef_sdk_version=row['cef_version'], platform=row['platform'], architecture=row['architecture'])
    if any(manifest.get(k) != v for k, v in expected.items()):
        raise ValueError('Packaged SDK version/target does not match build plan')
    if package.name != sdk_filename(plan, row):
        raise ValueError('SDK filename does not match XCef package naming')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    name = package.name
    shutil.copyfile(package, output / name)
    add_package_record(output / name, row)
    metadata = dict(expected, cef_major=row['cef_major'], file=name,
                    sha256=digest(output / name), size=(output / name).stat().st_size,
                    source_sha=plan['source_sha'], build_sha=plan['build_sha'])
    write_json(output / Path(name).with_suffix('.json'), metadata)


def assemble(plan, directory):
    directory = Path(directory)
    assets = []
    for row in plan['matrix']:
        name = sdk_filename(plan, row)
        item = json.loads((directory / Path(name).with_suffix('.json')).read_text())
        expected = dict(file=name, source_sha=plan['source_sha'], build_sha=plan['build_sha'],
                        xcef_version=plan['xcef_version'], cef_sdk_version=row['cef_version'],
                        cef_major=row['cef_major'], platform=row['platform'], architecture=row['architecture'])
        if any(item.get(k) != v for k, v in expected.items()):
            raise ValueError('Artifact metadata does not match build plan')
        if digest(directory / name) != item['sha256'] or (directory / name).stat().st_size != item['size']:
            raise ValueError('Artifact checksum/size mismatch')
        with zipfile.ZipFile(directory / name) as archive:
            check_package_record(archive, package_record_path(row, archive.namelist()), Path(name).stem)
        assets.append(item)
    if {p.name for p in directory.glob('*.zip')} != {a['file'] for a in assets}:
        raise ValueError('Unexpected packages')
    write_catalog(plan, assets, directory)
    return assets


def finalize(args):
    plan = json.loads(os.environ['BUILD_PLAN'])
    if os.environ.get('BUILD_TARGET'):
        row = json.loads(os.environ['BUILD_TARGET'])
        if row not in plan['matrix']:
            raise ValueError('Publish target is not in the build plan')
        plan = dict(plan, matrix=[row])
    assets = assemble(plan, args.directory)
    info = release_info(plan['release_tag'])
    if info:
        finalize_existing(info, plan, assets, Path(args.directory))
        return
    directory = Path(args.directory)
    write_json(directory / 'build-plan.json', plan)
    notes = directory / 'notes.md'
    lines = [f"Source: [{plan['source_tag']}](https://github.com/{SOURCE_REPO}/tree/{plan['source_sha']})",
             f"Source commit: `{plan['source_sha']}`", f"Build commit: `{plan['build_sha']}`",
             '', 'Packages are published independently after checksum verification.',
             'See manifest.json for the current package inventory.',
             'Build success is not a runtime compatibility certification.']
    notes.write_text('\n\n'.join(lines[:6]) + '\n' + '\n'.join(lines[6:]) + '\n', encoding='utf-8')
    run('gh', 'release', 'create', plan['release_tag'], *(str(directory / a['file']) for a in assets),
        str(directory / 'build-plan.json'), str(directory / 'manifest.json'), str(directory / 'SHA256SUMS'),
        '--target', plan['build_sha'], '--title', f"XCef {plan['xcef_version']}", '--notes-file', str(notes))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare_parser = commands.add_parser('prepare')
    prepare_parser.add_argument('--source', default='source')
    stage = commands.add_parser('stage')
    stage.add_argument('--packages', required=True)
    stage.add_argument('--output', required=True)
    final = commands.add_parser('finalize')
    final.add_argument('--directory', required=True)
    args = parser.parse_args()
    if args.command == 'prepare':
        prepare(args)
    elif args.command == 'stage':
        stage_package(json.loads(os.environ['BUILD_PLAN']), json.loads(os.environ['BUILD_TARGET']), args.packages, args.output)
    else:
        finalize(args)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
