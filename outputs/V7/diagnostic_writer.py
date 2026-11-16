"""Latest-only diagnostic I/O, never an input queue. Standard library only."""
from pathlib import Path
import json
import os
import shutil
import threading
import time
import uuid

REVISION = 'async-diagnostics-20261109'


def atomic_stream_json(path, document, chunk_chars=65536):
    """Bound memory/GIL bursts, preserve one previous file, replace atomically.

    This is for rolling diagnostics, not user settings/source backups. Yielding
    between chunks is cooperative, not a hard real-time scheduling guarantee.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    previous_tmp = None
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            chunks, size = [], 0
            for chunk in json.JSONEncoder(ensure_ascii=False, indent=2).iterencode(document):
                chunks.append(chunk)
                size += len(chunk)
                if size >= chunk_chars:
                    stream.write(''.join(chunks))
                    chunks, size = [], 0
                    time.sleep(0)
            stream.write(''.join(chunks) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            backup = path.parent / '.backups'
            backup.mkdir(exist_ok=True)
            previous = backup / (path.name + '.previous')
            previous_tmp = previous.with_name(previous.name + '.' + uuid.uuid4().hex + '.tmp')
            shutil.copy2(path, previous_tmp)
            os.replace(previous_tmp, previous)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
        if previous_tmp is not None:
            previous_tmp.unlink(missing_ok=True)


def write_documents(payload):
    data, trace, root = payload
    atomic_stream_json(root / '.settings' / 'runtime-diagnostic.json', data)
    if trace:
        atomic_stream_json(root / 'verification' / 'control-trace.json',
            [dict(item, session_id=data['session_id'], diagnostic_sequence=data['diagnostic_sequence'])
             for item in trace])


class DiagnosticWriter:
    """One active + one replaceable pending snapshot. No Tk/device calls.

    submit transfers an owned snapshot; do not mutate it afterwards. Disk stalls
    never create a FIFO backlog. Closing drains the latest snapshot off the UI.
    """
    def __init__(self, sink=write_documents):
        self.sink = sink
        self._condition = threading.Condition()
        self._pending = None
        self._closed = False
        self._thread = None
        self._submitted = self._written = self._replaced = self._failed = 0
        self._last_written_sequence = 0
        self._active = False
        self._last_error = None
        self._last_write_ms = None
        self._last_submit_at = self._last_written_at = None

    def submit(self, payload):
        with self._condition:
            if self._closed:
                return False
            if self._thread is None:
                thread = threading.Thread(target=self._run, name='V7Diagnostics', daemon=True)
                # Start before publishing: a start failure must not leave a job
                # permanently pending, or pretend the submission succeeded.
                thread.start()
                self._thread = thread
            if self._pending is not None:
                self._replaced += 1
            self._submitted += 1
            self._pending = (self._submitted, payload)
            self._last_submit_at = time.monotonic()
            self._condition.notify()
            return True

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending is not None or self._closed)
                if self._pending is None:
                    return
                sequence, payload = self._pending
                self._pending = None
                self._active = True
            started = time.monotonic()
            error = None
            try:
                self.sink(payload)
            except Exception as exc:
                error = f'{type(exc).__name__}: {exc}'
            with self._condition:
                self._active = False
                self._last_write_ms = round((time.monotonic()-started)*1000, 3)
                self._last_error = error
                if error is None:
                    self._written += 1
                    self._last_written_sequence = sequence
                    self._last_written_at = time.monotonic()
                else:
                    self._failed += 1
                self._condition.notify_all()

    def close(self):
        """Call ONLY from background shutdown, not a Tk callback."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join()

    def snapshot(self):
        with self._condition:
            return dict(revision=REVISION, submitted=self._submitted, written=self._written,
                pending=self._pending is not None, active=self._active, closed=self._closed,
                replaced_pending=self._replaced, failures=self._failed, last_error=self._last_error,
                last_written_sequence=self._last_written_sequence, last_write_ms=self._last_write_ms,
                seconds_since_write=(round(time.monotonic()-self._last_written_at, 3)
                                     if self._last_written_at is not None else None),
                capacity='one active + one latest pending diagnostic; no input reports queued')


def status_text(state):
    text = '诊断异步：可能延后/合并旧诊断，不合并输入；索引加速不改力度'
    if state['last_error']:
        return text + '【写入失败：' + state['last_error'] + '】'
    if state['pending'] or state['active']:
        return text + f" · 待写（已合并 {state['replaced_pending']}）"
    return text + f" · 已写 {state['written']} 次"
