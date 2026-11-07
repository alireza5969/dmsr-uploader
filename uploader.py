#!/usr/bin/env python3
"""Metadata-only source discovery; bounded downloads; durable upload journal."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

STATE = Path('state')
LIMIT = 50_000_000  # conservative interpretation of Telegram's 50 MB limit


def log(message):
    print(message, flush=True)


def git(*args, cwd=None, env=None):
    r = subprocess.run(['git', *args], cwd=cwd, env=env, capture_output=True, timeout=180)
    if r.returncode:
        # Never include subprocess output: authentication data could appear there.
        raise RuntimeError('Git operation failed: ' + args[0])
    return r.stdout


def checkpoint():
    if not STATE.exists():
        return
    start = time.monotonic()
    git('add', '--', 'state')
    r = subprocess.run(['git', 'diff', '--cached', '--quiet'], capture_output=True)
    if r.returncode == 1:
        git('commit', '-m', 'uploader: checkpoint')
    elif r.returncode:
        raise RuntimeError('Cannot inspect staged progress')
    # Only the small uploader repo is fetched. Never force-push.
    git('pull', '--rebase')
    git('push')
    log(f'checkpoint_seconds={time.monotonic()-start:.2f}')


def source_snapshot(repo, branch, directory):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise ValueError('Invalid SOURCE_REPO')
    env = os.environ.copy()
    env['GIT_TERMINAL_PROMPT'] = '0'
    token = env.get('SOURCE_TOKEN')
    if token:
        auth = base64.b64encode(('x-access-token:' + token).encode()).decode()
        env.update(GIT_CONFIG_COUNT='1',
                   GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',
                   GIT_CONFIG_VALUE_0='Authorization: Basic ' + auth)
    git('clone', '--quiet', '--filter=blob:none', '--no-checkout', '--depth=1',
        '--single-branch', '--branch', branch, '--',
        f'https://github.com/{repo}.git', str(directory), env=env)
    commit = git('rev-parse', 'HEAD', cwd=directory).decode().strip()
    # Do NOT add -l: requesting blob sizes would fetch file contents lazily.
    records = git('ls-tree', '-rz', '--full-tree', commit, '--', 'data/', cwd=directory)
    files = []
    for record in records.split(b'\0'):
        if not record:
            continue
        header, name = record.split(b'\t', 1)
        mode, kind, sha = header.decode().split()
        path = name.decode('utf-8')
        if kind == 'blob' and mode in ('100644', '100755'):
            files.append({'path': path, 'sha': sha})
    return commit, files


def fetch(repo, commit, path, limit=LIMIT):
    token = os.getenv('SOURCE_TOKEN')
    if token:
        url = f'https://api.github.com/repos/{repo}/contents/{urllib.parse.quote(path)}?ref={commit}'
    else:
        url = f'https://raw.githubusercontent.com/{repo}/{commit}/{urllib.parse.quote(path)}'
    headers = {'User-Agent': 'dmsr-uploader', 'Accept': 'application/vnd.github.raw+json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    # GETs are safe to retry; POSTs to Telegram are handled separately.
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as r:
                if int(r.headers.get('Content-Length', '0')) > limit:
                    raise OverflowError('File exceeds configured limit')
                data = r.read(limit + 1)
                if len(data) > limit:
                    raise OverflowError('File exceeds configured limit')
                return data
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise RuntimeError(f'Source HTTP {e.code}') from None
        except (OSError, TimeoutError):
            if attempt == 2:
                raise RuntimeError('Source download failed') from None
        time.sleep(2 ** attempt)


def blob_sha(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def reviewer(n):
    for low, high, label in [(1, 6164, 'fzm'), (6165, 12012, 'ajk'),
                             (12013, 17937, 'mas'), (17938, 23968, 'kf')]:
        if low <= n <= high:
            return label
    return 'unassigned'


def inventory(files):
    groups = {}
    for entry in files:
        p = PurePosixPath(entry['path'])
        if re.fullmatch(r'[0-9]+\.PMC[0-9]+', p.parent.name):
            groups.setdefault(str(p.parent), []).append(entry)
    result = []
    for directory in sorted(groups):
        group = sorted(groups[directory], key=lambda x: x['path'])
        n, pmcid = PurePosixPath(directory).name.split('.')
        for i, entry in enumerate(group, 1):
            result.append(dict(entry, caption=f'number: #{n}\nreviewer: #{reviewer(int(n))}\n'
                               f'PMC ID: #{pmcid}\nSequence number: {i}/{len(group)}'))
    return result


def load_state():
    latest = {}
    journal = STATE / 'uploads.jsonl'
    if journal.exists():
        for line in journal.read_text().splitlines():
            event = json.loads(line)  # fail closed on a damaged journal
            latest[event['path']] = event
    return latest


def record(entry, status, **extra):
    STATE.mkdir(exist_ok=True)
    event = {k: entry[k] for k in ('path', 'sha')}
    event.update(status=status, time=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), **extra)
    with (STATE / 'uploads.jsonl').open('a') as f:
        f.write(json.dumps(event, ensure_ascii=True) + '\n')
        f.flush()
        os.fsync(f.fileno())
    return event


def pending(entry, old, legacy):
    if entry['path'] in legacy:
        return False  # legacy cache records paths, not content hashes
    event = old.get(entry['path'])
    if not event:
        return True
    if event['status'] in ('inflight', 'uncertain'):
        return False  # requires human reconciliation, even if content changed
    return event['sha'] != entry['sha'] or event['status'] == 'failed'


def multipart(chat, caption, filename, data):
    boundary = uuid.uuid4().hex
    chunks = []
    for name, value in [('chat_id', chat), ('caption', caption)]:
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    # Escape MIME header delimiters; preserve normal Unicode filenames.
    filename = filename.replace('\\', '_').replace('"', '_').replace('\r', '_').replace('\n', '_')
    chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode())
    chunks.extend([data, f'\r\n--{boundary}--\r\n'.encode()])
    return boundary, b''.join(chunks)


def send(entry, data, deadline):
    boundary, body = multipart(os.environ['CHAT_ID'], entry['caption'],
                               PurePosixPath(entry['path']).name, data)
    url = 'https://api.telegram.org/bot' + os.environ['TG_TOKEN'] + '/sendDocument'
    for attempt in range(5):
        if time.monotonic() + 610 > deadline:
            return 'failed', {'reason': 'budget_before_request'}
        request = urllib.request.Request(url, data=body,
                    headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
        try:
            with urllib.request.urlopen(request, timeout=600) as r:
                out = json.load(r)
        except urllib.error.HTTPError as e:
            try:
                out = json.loads(e.read())
            except Exception:
                out = {'error_code': e.code}
        except Exception:
            # A lost response may mean Telegram accepted the document. Do not resend.
            return 'uncertain', {'reason': 'response_lost'}
        if out.get('ok'):
            message = out['result']
            return 'sent', {'message_id': message['message_id'],
                            'file_id': message.get('document', {}).get('file_id')}
        code = out.get('error_code', 0)
        if code == 429:
            wait = max(3.1, float(out.get('parameters', {}).get('retry_after', 60)) + 1)
            if attempt == 4 or time.monotonic() + wait + 610 > deadline:
                return 'failed', {'reason': 'rate_limit'}
            log(f'telegram_rate_limit_wait_seconds={wait}')
            time.sleep(wait)
            continue
        if 400 <= code < 500:
            return 'failed', {'reason': f'telegram_{code}'}
        return 'uncertain', {'reason': 'telegram_server_or_invalid_response'}
    raise AssertionError('unreachable')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', action='store_true', help='Send documents; default is read-only plan')
    parser.add_argument('--checkpoint', action='store_true')
    args = parser.parse_args()
    if args.checkpoint:
        checkpoint()
        return
    repo = os.getenv('SOURCE_REPO', 'alireza5969/dmsr-supp')
    branch = os.getenv('SOURCE_BRANCH', 'main')
    chat = os.getenv('CHAT_ID', '')
    if args.run and (not chat or not os.getenv('TG_TOKEN')):
        raise ValueError('TG_TOKEN and CHAT_ID are required')
    deadline = time.monotonic() + float(os.getenv('TIME_BUDGET_MIN', '300')) * 60
    meta_path = STATE / 'metadata.json'
    identity = {'source_repo': repo, 'chat_id': chat}
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
        if meta['source_repo'] != repo or (chat and meta['chat_id'] != chat):
            raise ValueError('State belongs to a different source or chat')
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='dmsr-source-') as temp:
        commit, files = source_snapshot(repo, branch, Path(temp) / 'source')
    log(f'source_commit={commit} discovery_seconds={time.monotonic()-start:.2f} files={len(files)}')
    legacy_path = STATE / 'legacy_uploaded.txt'
    if meta_path.exists() and not legacy_path.exists():
        raise ValueError('Missing legacy cache; restore state before continuing')
    # First-run migration MUST succeed, otherwise do not send anything.
    legacy_bytes = (legacy_path.read_bytes() if legacy_path.exists()
                    else fetch(repo, commit, 'upload_cache.txt', limit=20_000_000))
    legacy = {p.strip().removeprefix('./').replace('\\', '/') for p in legacy_bytes.decode().splitlines() if p.strip()}
    old = load_state()
    entries = inventory(files)
    queue = [e for e in entries if pending(e, old, legacy)]
    held = sum(e['status'] in ('inflight', 'uncertain') for e in old.values())
    log(f'eligible={len(entries)} legacy_uploaded={len(legacy)} pending={len(queue)} held_for_review={held}')
    if not args.run:
        for entry in queue[:5]:
            log(json.dumps(entry))
        return
    STATE.mkdir(exist_ok=True)
    if not meta_path.exists():
        legacy_path.write_bytes(legacy_bytes)
        meta_path.write_text(json.dumps(dict(identity, imported_from_commit=commit), indent=2) + '\n')
    checkpoint()  # verify persistence works BEFORE any Telegram side effect
    maximum = int(os.getenv('MAX_FILES', '0'))  # zero = all within time budget
    attempts = 0
    failures = 0
    try:
        for entry in queue:
            if time.monotonic() + 750 > deadline or (maximum and attempts >= maximum):
                break
            if entry['sha'] == blob_sha(b''):
                record(entry, 'skipped', reason='empty_file')
                continue
            start = time.monotonic()
            try:
                data = fetch(repo, commit, entry['path'])
                if data.startswith(b'version https://git-lfs.github.com/spec/v1\n'):
                    record(entry, 'skipped', reason='git_lfs_not_supported')
                    continue
                if blob_sha(data) != entry['sha']:
                    raise RuntimeError('Downloaded blob hash mismatch')
            except OverflowError:
                record(entry, 'skipped', reason='over_50_MB')
                continue
            except RuntimeError:
                record(entry, 'failed', reason='source_download_or_hash_failed')
                failures += 1
                if failures >= 5:
                    raise RuntimeError('Five consecutive failures; inspect journal')
                continue
            download_seconds = time.monotonic() - start
            record(entry, 'inflight')
            start = time.monotonic()
            status, details = send(entry, data, deadline)
            del data
            record(entry, status, **details)
            attempts += 1
            failures = 0 if status == 'sent' else failures + 1
            log(json.dumps({'path': entry['path'], 'status': status,
                            'download_seconds': round(download_seconds, 2),
                            'telegram_seconds': round(time.monotonic()-start, 2)}))
            if attempts % 25 == 0:
                checkpoint()
            if status == 'uncertain' or failures >= 5:
                raise RuntimeError('Upload stopped; inspect state/uploads.jsonl')
            time.sleep(3.1)
    finally:
        checkpoint()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        # Exception strings from network libraries can contain the bot token.
        log('ERROR: ' + (str(exc) if isinstance(exc, (RuntimeError, ValueError)) else type(exc).__name__))
        raise SystemExit(1)
