"""Explicit, private export of local SQLite annotations for the team dashboard.

No abstracts, bibliographic inventory, deleted DOIs, or database credentials.
The export is never automatically published or synchronized.
"""
import json
import os
from pathlib import Path

from .db import now

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_DIRECTORIES = ('docs', 'cloud/public', 'public_deploy', 'site', '.github')


def export_local_state(conn, output='data/private/team-state.json'):
    target = Path(output).expanduser().resolve()
    for folder in PUBLIC_DIRECTORIES:
        if target.is_relative_to((ROOT / folder).resolve()):
            raise ValueError('Refusing to write private annotations into a public or source asset directory')
    if target.suffix.lower() != '.json':
        raise ValueError('Backup filename must end in .json')
    if target.exists():
        raise FileExistsError('Private backup already exists; choose a new --output filename')
    annotations = []
    long_notes = 0
    for row in conn.execute("SELECT doi,rating,note,acknowledged,hidden FROM user_articles WHERE user_id='local' ORDER BY doi"):
        note = row['note'] or ''
        if len(note) > 4000:
            long_notes += 1
        annotations.append({'doi': row['doi'], 'rating': row['rating'], 'note': note,
                            'acknowledged': bool(row['acknowledged']), 'hidden': bool(row['hidden']),
                            'share_note': False})
    manual_papers = [dict(row) for row in conn.execute(
        'SELECT doi,title,COALESCE(journal,\'\') AS journal FROM articles WHERE manually_added=1 ORDER BY doi')]
    data = {'format': 'literature-tracker-user-state', 'schema_version': 1,
            'exported_at': now(), 'source': 'local-sqlite',
            'annotations': annotations, 'manual_papers': manual_papers}
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive create prevents accidental replacement of an earlier private backup.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return {'output': str(target), 'annotations': len(annotations),
            'manual_papers': len(manual_papers), 'notes_exceeding_cloud_limit': long_notes,
            'private_file': True}
