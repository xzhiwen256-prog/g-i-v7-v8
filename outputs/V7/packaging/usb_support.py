"""Portable installer primitives. No driver, input, model or native API calls."""
from pathlib import Path, PurePosixPath
import hashlib
import json
import os
import re
import shutil


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


def safe_relative(value):
    if not isinstance(value, str) or not value or '\\' in value or ':' in value:
        raise ValueError('Invalid package path')
    p = PurePosixPath(value)
    if p.is_absolute() or any(part in ('..', '.') for part in value.split('/')) or '' in value.split('/'):
        raise ValueError('Unsafe package path')
    return Path(*p.parts)


def under(root, path):
    root, path = Path(root).resolve(), Path(path).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError('Path escapes the owned product directory')
    return path


def no_links(root, path):
    root = Path(root).absolute()
    path = Path(path).absolute()
    if not path.is_relative_to(root):
        raise ValueError('Path outside package')
    for p in (root, *reversed(path.parents), path):
        if p != root and not p.is_relative_to(root):
            continue
        if p.is_symlink() or p.is_junction():
            raise ValueError('Linked package/cache paths are not supported')


def load_manifest(root):
    root = Path(root)
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError('Duplicate manifest key')
            out[key] = value
        return out
    raw = (root/'MANIFEST.json').read_bytes()
    m = json.loads(raw, object_pairs_hook=unique)
    if m.get('schema') != 1 or not re.fullmatch(r'V7-USB-[0-9]{8}-[a-z0-9-]+', m.get('release_id', '')):
        raise ValueError('Unsupported package manifest')
    if not isinstance(m.get('files'), dict) or not m['files']:
        raise ValueError('Empty package')
    seen = set()
    for name, entry in m['files'].items():
        safe_relative(name)
        if name.casefold() in seen:
            raise ValueError('Case-colliding package paths')
        seen.add(name.casefold())
        if type(entry.get('bytes')) is not int or entry['bytes'] < 0 or not re.fullmatch(r'[0-9a-f]{64}', entry.get('sha256', '')):
            raise ValueError('Invalid package file record')
    if not re.fullmatch(r'[0-9a-f]{64}', m.get('driver_component_sha256', '')):
        raise ValueError('Missing component fingerprint')
    if not isinstance(m.get('critical_files'),list) or any(n not in m['files'] for n in m['critical_files']):
        raise ValueError('Invalid critical file list')
    return m, hashlib.sha256(raw).hexdigest()


def install_payload(source, product, manifest, manifest_hash, progress=lambda *args: None, cancelled=lambda: False):
    """Resumable, versioned local copy. Never recursively deletes anything."""
    no_links(source,source); no_links(product,product)
    source, product = Path(source).resolve(), Path(product).resolve()
    if product == source or product.is_relative_to(source):
        raise ValueError('Install cache must be outside the USB package')
    versions = under(product, product/'versions')
    versions.mkdir(parents=True, exist_ok=True)
    no_links(product,product/'versions'/manifest['release_id'])
    final = under(versions, versions/manifest['release_id'])
    no_links(product, final)
    marker = final/'INSTALL-COMPLETE.json'
    if marker.exists():
        state = json.loads(marker.read_text('utf-8'))
        if state.get('manifest_sha256') != manifest_hash:
            raise ValueError('Existing version does not match the USB package; do not overwrite it')
        for name in manifest['critical_files']:
            relative = safe_relative(name)
            if relative.parts[0] != 'payload':
                continue
            path = final/Path(*relative.parts[1:])
            no_links(product, path)
            if not path.is_file() or digest(path) != manifest['files'][name]['sha256']:
                raise ValueError('Installed critical file is missing or altered: '+name)
        progress('本机配置已存在，校验完成', 1.)
        return final
    if final.exists():
        raise ValueError('Incomplete final version exists; use a new release directory')
    no_links(product,versions/('.pending-'+manifest['release_id']))
    staging = under(versions, versions/('.pending-'+manifest['release_id']))
    no_links(product, staging)
    staging.mkdir(exist_ok=True)
    files = [(n, e) for n, e in manifest['files'].items() if n.startswith('payload/')]
    total = sum(e['bytes'] for _, e in files)
    staged_bytes = sum(e['bytes'] for n,e in files if (staging/Path(*safe_relative(n).parts[1:])).is_file() and (staging/Path(*safe_relative(n).parts[1:])).stat().st_size==e['bytes'])
    required = total-staged_bytes + max((e['bytes'] for _,e in files),default=0) + 512*2**20
    if shutil.disk_usage(product).free < required:
        raise OSError(f'本地磁盘空间不足，本次配置至少还需要约 {required/2**30:.1f} GB')
    done = 0
    for name, entry in files:
        if cancelled():
            raise InterruptedError('配置已取消，尚未安装输入组件；下次可继续复制')
        relative = safe_relative(name)
        no_links(source,source/relative);no_links(product,staging/Path(*relative.parts[1:]))
        src = under(source, source/relative)
        dst = under(staging, staging/Path(*relative.parts[1:]))
        no_links(source, src); no_links(product, dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_file() and dst.stat().st_size == entry['bytes'] and digest(dst) == entry['sha256']:
            done += entry['bytes']; progress('继续校验本机文件', done/max(1,total)); continue
        temporary = dst.with_name(dst.name+'.copying')
        no_links(product, temporary)
        h, size = hashlib.sha256(), 0
        with src.open('rb') as reader, temporary.open('wb') as writer:
            while chunk := reader.read(4*2**20):
                if cancelled():
                    raise InterruptedError('复制已暂停，下次可继续')
                writer.write(chunk); h.update(chunk); size += len(chunk)
                progress('首次配置：复制并校验运行环境', (done+size)/max(1,total))
        if size != entry['bytes'] or h.hexdigest() != entry['sha256']:
            raise ValueError('U盘文件不完整或已被修改：'+name)
        os.replace(temporary, dst)
        done += size
    atomic_json(staging/'INSTALL-COMPLETE.json', dict(manifest_sha256=manifest_hash, release_id=manifest['release_id']))
    # Resolve and check both absolute paths immediately before the directory rename.
    under(versions, staging); under(versions, final)
    no_links(product, staging); no_links(product, final)
    os.rename(staging, final)
    progress('本机文件配置完成', 1.)
    return final


def accepted_consent(product, component_hash):
    path = Path(product)/'consents'/(component_hash+'.json')
    try:
        no_links(product, path)
        data = json.loads(path.read_text('utf-8'))
        return data.get('accepted') is True and data.get('driver_component_sha256') == component_hash and data.get('consent_version') == 1
    except (OSError, ValueError, TypeError):
        return False
