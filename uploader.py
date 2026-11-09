#!/usr/bin/env python3
"""Metadata-only source discovery; bounded downloads; durable upload journal."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import random
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
# Each job is assigned to one reviewer and its own Telegram channel.
REVIEWER_KEY = os.getenv('REVIEWER', '').strip().lower()
REVIEWERS = {'fzm', 'ajk', 'mas', 'kf'}
# Minimum gap between the *starts* of two Telegram send requests from one bot.
SEND_INTERVAL = float(os.getenv('SEND_INTERVAL', '3.1'))
PUSH_ATTEMPTS = 6


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
        suffix = f' ({REVIEWER_KEY})' if REVIEWER_KEY else ''
        git('commit', '-m', 'uploader: checkpoint' + suffix)
    elif r.returncode:
        raise RuntimeError('Cannot inspect staged progress')
    # Only the small uploader repo is fetched. Never force-push. Parallel shards push to
    # the same branch, so a rejected push is expected: re-sync and retry with jitter.
    # Shards write different journal files, so the rebase itself should not conflict.
    for attempt in range(PUSH_ATTEMPTS):
        try:
            git('pull', '--rebase')
            git('push')
            break
        except RuntimeError:
            subprocess.run(['git', 'rebase', '--abort'], capture_output=True)
            if attempt == PUSH_ATTEMPTS - 1:
                raise
            time.sleep(random.uniform(2, 10))
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


def mine(entry):
    """True if this reviewer job owns the article's fixed number range."""
    number = int(PurePosixPath(entry['path']).parent.name.split('.')[0])
    return reviewer(number) == REVIEWER_KEY


def journal_path():
    # Each reviewer has a distinct journal; concurrent jobs never edit the same file.
    return STATE / f'uploads.reviewer.{REVIEWER_KEY}.jsonl'


def journal_files():
    files = []
    original = STATE / 'uploads.jsonl'
    if original.exists():
        files.append(original)
    files.extend(sorted(STATE.glob('uploads.shard*.jsonl')))  # legacy modulo-shard journals
    files.extend(sorted(STATE.glob('uploads.reviewer.*.jsonl')))
    return files


def load_state():
    events = []
    for journal in journal_files():
        for line in journal.read_text().splitlines():
            events.append(json.loads(line))  # fail closed on a damaged journal
    # Journals are merged by timestamp so the newest event for a path wins even if the
    # shard count changed between runs and a path moved to another shard's file.
    # sorted() is stable, so events with equal timestamps keep their file order.
    events.sort(key=lambda event: event.get('time', ''))
    latest = {}
    for event in events:
        latest[event['path']] = event
    return latest


def record(entry, status, **extra):
    STATE.mkdir(exist_ok=True)
    event = {k: entry[k] for k in ('path', 'sha')}
    event.update(status=status, time=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), **extra)
    if os.getenv('CHAT_ID'):
        event['chat_id'] = os.environ['CHAT_ID']
    with journal_path().open('a') as f:
        f.write(json.dumps(event, ensure_ascii=True) + '\n')
        f.flush()
        os.fsync(f.fileno())
    return event


def validate_reviewer_destination(meta, reviewer, chat):
    """Protect the existing shared channel from accidental re-import duplicates."""
    original_chat = meta.get('chat_id')
    if reviewer == 'fzm' and chat and original_chat and chat != original_chat:
        raise ValueError('CHAT_ID_FZM must match the existing channel recorded in state/metadata.json')


def covered_by_existing_fzm_channel(entry, reviewer=REVIEWER_KEY):
    """The existing fzm channel is complete through article 04443, inclusive."""
    if reviewer != 'fzm':
        return False
    number = int(PurePosixPath(entry['path']).parent.name.split('.')[0])
    return 1 <= number <= 4443


def pending(entry, old, legacy, chat=None, legacy_chat=None):
    chat = chat if chat is not None else os.getenv('CHAT_ID', '')
    # Previous success records belong to their original destination only.
    if entry['path'] in legacy and legacy_chat == chat:
        return False
    event = old.get(entry['path'])
    if not event or event.get('chat_id') != chat:
        return True
    if event['status'] in ('inflight', 'uncertain'):
        return False  # requires human reconciliation, even if content changed
    return event['sha'] != entry['sha'] or event['status'] == 'failed'


def pace(last_send, interval=SEND_INTERVAL):
    """Sleep only for what remains of `interval` since the previous send started.

    Download time and journaling already count towards the gap, so they no longer
    stack on top of a fixed sleep. Returns the new send-start timestamp.
    """
    wait = last_send + interval - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    return time.monotonic()


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
    if REVIEWER_KEY not in REVIEWERS:
        raise ValueError('REVIEWER must be one of: fzm, ajk, mas, kf')
    repo = os.getenv('SOURCE_REPO', 'alireza5969/dmsr-supp')
    branch = os.getenv('SOURCE_BRANCH', 'main')
    chat = os.getenv('CHAT_ID', '')
    if args.run and (not chat or not os.getenv('TG_TOKEN')):
        raise ValueError('TG_TOKEN and CHAT_ID are required')
    deadline = time.monotonic() + float(os.getenv('TIME_BUDGET_MIN', '300')) * 60
    meta_path = STATE / 'metadata.json'
    identity = {'source_repo': repo, 'chat_id': chat}
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    if meta_path.exists() and meta.get('source_repo') != repo:
        raise ValueError('State belongs to a different source repository')
    validate_reviewer_destination(meta, REVIEWER_KEY, chat)
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
    legacy_chat = meta.get('chat_id')
    old = load_state()
    entries = inventory(files)
    owned = [e for e in entries if mine(e)]
    queue = [e for e in owned if not covered_by_existing_fzm_channel(e) and
             pending(e, old, legacy, chat=chat, legacy_chat=legacy_chat)]
    held = sum(e['status'] in ('inflight', 'uncertain') for e in old.values())
    protected = sum(covered_by_existing_fzm_channel(e) for e in owned)
    log(f'reviewer={REVIEWER_KEY} eligible={len(entries)} reviewer_eligible={len(owned)} '
        f'protected_existing_channel={protected} legacy_uploaded={len(legacy)} '
        f'pending={len(queue)} held_for_review={held}')
    if not args.run:
        for entry in queue[:5]:
            log(json.dumps(entry))
        return
    STATE.mkdir(exist_ok=True)
    if not meta_path.exists():
        legacy_path.write_bytes(legacy_bytes)
        meta_path.write_text(json.dumps(dict(identity, imported_from_commit=commit), indent=2) + '\n')
    checkpoint()  # verify persistence works BEFORE any Telegram side effect
    maximum = int(os.getenv('MAX_FILES', '0'))  # zero = all within time budget; per shard
    attempts = 0
    failures = 0
    last_send = float('-inf')
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
            # Pace before journaling 'inflight' so an interruption while waiting
            # does not leave an entry held for manual review.
            last_send = pace(last_send)
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
                raise RuntimeError('Upload stopped; inspect the shard journal in state/')
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
