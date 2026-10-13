"""Atomic success evidence, not a queue or task history."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid
from .ledger import fsync_directory


def marker_path(database):
    return Path(str(database) + '.last_verified_sync.json')

def revision(db):
    h = hashlib.sha256()
    for sql in ('SELECT * FROM evidence ORDER BY id',
                'SELECT * FROM observations ORDER BY endpoint,source_id,digest',
                'SELECT * FROM sightings ORDER BY endpoint,source_id,evidence_id',
                'SELECT * FROM notes ORDER BY id',
                'SELECT * FROM ranges ORDER BY endpoint,start,stop',
                'SELECT * FROM gaps ORDER BY id',
                'SELECT * FROM cycle_aliases ORDER BY legacy'):
        for row in db.execute(sql):
            h.update(json.dumps(list(row), ensure_ascii=False, separators=(',', ':')).encode())
            h.update(b'\n')
    return h.hexdigest()


def request_id(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError('Invalid request identifier')
    return value


def read_marker(database, db):
    path = marker_path(database)
    try:
        if not path.exists():
            return None
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 2048:
            return None
        data = json.loads(path.read_text())
        if set(data) != {'request_id','cutoff','checked_at','revision'}:
            return None
        request_id(data['request_id'])
        if any(type(data[k]) is not int or data[k] < 1788192000 for k in ('cutoff','checked_at')) or data['checked_at'] < data['cutoff']:
            return None
        if not re.fullmatch('[a-f0-9]{64}', data['revision']):
            return None
        return dict(data, matches_current_ledger=(data['revision'] == revision(db)))
    except (OSError, ValueError, TypeError, KeyError):
        return None


def write_marker(database, data):
    target = marker_path(database)
    fd, temporary = tempfile.mkstemp(dir=target.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(data, out, separators=(',', ':'))
            out.flush(); os.fsync(out.fileno())
        os.replace(temporary, target)
        fsync_directory(target.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)
