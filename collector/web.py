"""Local-only, standard-library HTTP dashboard backed by the existing SQLite DB.

All user notes and ratings remain in SQLite; the public collector export never
contains them unless explicitly requested by the CLI. No cloud service needed.
"""
import json
from contextlib import closing
import re
import sqlite3
import tempfile
import threading
import urllib.error
import urllib.parse
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .core import classify, crossref_doi, normalize_doi
from .db import connect, now, set_user_state, upsert_article

ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = ROOT / 'web'
MAX_BODY = 65536


def _bounded_int(value, default, low, high):
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(result, low), high)


def _like(value):
    return '%' + value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


def _state_join():
    return "FROM articles a LEFT JOIN user_articles u ON u.doi=a.doi AND u.user_id='local'"


def _article_select():
    return ("SELECT a.*, COALESCE(u.acknowledged,0) AS acknowledged, "
            "COALESCE(u.hidden,0) AS hidden, u.rating AS rating, "
            "COALESCE(u.note,'') AS note " + _state_join())


def _row_to_article(row):
    item = dict(row)
    item['authors'] = json.loads(item.pop('authors_json'))
    item['issns'] = json.loads(item.pop('issns_json'))
    return item


def _stats(conn):
    sql = """SELECT COUNT(*) AS total,
      COALESCE(SUM(CASE WHEN a.decision!='reject' AND COALESCE(u.hidden,0)=0 THEN 1 ELSE 0 END),0) AS inbox,
      COALESCE(SUM(CASE WHEN a.decision='review' AND COALESCE(u.hidden,0)=0 THEN 1 ELSE 0 END),0) AS review,
      COALESCE(SUM(CASE WHEN u.rating IS NOT NULL OR TRIM(COALESCE(u.note,''))!='' THEN 1 ELSE 0 END),0) AS rated,
      COALESCE(SUM(COALESCE(u.hidden,0)),0) AS hidden,
      COALESCE(SUM(CASE WHEN a.decision='reject' THEN 1 ELSE 0 END),0) AS rejected
      """ + _state_join()
    return dict(conn.execute(sql).fetchone())


def query_articles(conn, args):
    """Server-side pagination/filtering for large historical backfills."""
    view = args.get('view', ['inbox'])[0]
    if view not in ('inbox', 'review', 'rated', 'hidden', 'all'):
        raise ValueError('Unknown view')
    where, params = [], []
    if view == 'inbox':
        where += ["a.decision!='reject'", 'COALESCE(u.hidden,0)=0']
    elif view == 'review':
        where += ["a.decision='review'", 'COALESCE(u.hidden,0)=0']
    elif view == 'rated':
        where += ["(u.rating IS NOT NULL OR TRIM(COALESCE(u.note,''))!='')"]
    elif view == 'hidden':
        where += ['COALESCE(u.hidden,0)=1']

    journal = args.get('journal', [''])[0]
    if journal:
        where.append('a.journal=?')
        params.append(journal)
    decision = args.get('decision', [''])[0]
    if decision:
        if decision not in ('include', 'review', 'reject'):
            raise ValueError('Invalid decision')
        where.append('a.decision=?')
        params.append(decision)
    abstract = args.get('abstract', [''])[0]
    if abstract:
        if abstract not in ('available', 'missing'):
            raise ValueError('Invalid abstract filter')
        where.append("a.abstract_status=?")
        params.append(abstract)
    read = args.get('read', [''])[0]
    if read:
        if read not in ('read', 'unread'):
            raise ValueError('Invalid acknowledgment filter')
        where.append('COALESCE(u.acknowledged,0)=?')
        params.append(1 if read == 'read' else 0)
    rated = args.get('rating', [''])[0]
    if rated:
        if rated not in ('rated', 'unrated'):
            raise ValueError('Invalid rating filter')
        where.append('u.rating IS NOT NULL' if rated == 'rated' else 'u.rating IS NULL')
    recent = args.get('recent', [''])[0]
    if recent:
        if recent not in ('7', '30', '90'):
            raise ValueError('Invalid first-seen window')
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(recent))).isoformat()
        where.append('COALESCE(a.first_seen_at,a.added_at)>=?')
        params.append(cutoff)
    search = args.get('search', [''])[0].strip()
    if len(search) > 200:
        raise ValueError('Search must be at most 200 characters')
    if search:
        where.append("(a.title LIKE ? ESCAPE '\\' OR a.abstract LIKE ? ESCAPE '\\' "
                     "OR a.doi LIKE ? ESCAPE '\\' OR a.journal LIKE ? ESCAPE '\\' "
                     "OR a.authors_json LIKE ? ESCAPE '\\')")
        params.extend([_like(search)] * 5)
    predicate = ' WHERE ' + ' AND '.join(where) if where else ''
    order_by = {
        'discovered': 'COALESCE(a.first_seen_at,a.added_at) DESC, a.doi',
        'publication': "COALESCE(a.published_date,'') DESC, COALESCE(a.first_seen_at,a.added_at) DESC, a.doi",
        'title': 'a.title COLLATE NOCASE ASC, a.doi',
        'rating': 'COALESCE(u.rating,0) DESC, COALESCE(a.first_seen_at,a.added_at) DESC, a.doi',
    }
    sort = args.get('sort', ['discovered'])[0]
    if sort not in order_by:
        raise ValueError('Invalid sort')
    limit = _bounded_int(args.get('limit', ['30'])[0], 30, 1, 100)
    offset = _bounded_int(args.get('offset', ['0'])[0], 0, 0, 10000000)
    total = conn.execute('SELECT COUNT(*) ' + _state_join() + predicate, params).fetchone()[0]
    rows = conn.execute(_article_select() + predicate + ' ORDER BY ' + order_by[sort]
                        + ' LIMIT ? OFFSET ?', params + [limit, offset]).fetchall()
    journals = [r[0] for r in conn.execute("SELECT DISTINCT journal FROM articles WHERE journal IS NOT NULL AND journal!='' ORDER BY journal")]
    return {'items': [_row_to_article(r) for r in rows], 'total': total,
            'offset': offset, 'limit': limit, 'stats': _stats(conn), 'journals': journals}


def article_by_doi(conn, doi):
    row = conn.execute(_article_select() + ' WHERE a.doi=?', (doi,)).fetchone()
    return _row_to_article(row) if row else None


class LiteratureServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, db_path):
        super().__init__(address, LiteratureHandler)
        self.db_path = Path(db_path).resolve()


class LiteratureHandler(BaseHTTPRequestHandler):
    server_version = 'LiteratureTracker/2.0'

    def log_message(self, fmt, *args):
        # Do not log DOI query strings, notes or private article state.
        pass

    def _headers(self, status, mime, size, extra=None):
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(size))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        for key, val in (extra or {}).items():
            self.send_header(key, val)
        self.end_headers()

    def _json(self, value, status=200):
        data = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        self._headers(status, 'application/json; charset=utf-8', len(data))
        self.wfile.write(data)

    def _error(self, status, message):
        self._json({'error': message}, status)

    def _allowed_origin(self):
        host = self.headers.get('Host', '')
        allowed = {f'127.0.0.1:{self.server.server_port}',
                   f'localhost:{self.server.server_port}'}
        if host not in allowed:
            return False
        origin = self.headers.get('Origin')
        return origin is None or origin in {f'http://{x}' for x in allowed}

    def _body(self):
        if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
            raise ValueError('Content-Type must be application/json')
        try:
            size = int(self.headers.get('Content-Length', '0'))
        except ValueError as exc:
            raise ValueError('Invalid Content-Length') from exc
        if size < 1 or size > MAX_BODY:
            raise ValueError('Request body must be between 1 and 65536 bytes')
        try:
            data = json.loads(self.rfile.read(size))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError('Invalid JSON') from exc
        if not isinstance(data, dict):
            raise ValueError('Expected a JSON object')
        return data

    def _connect(self):
        conn = connect(self.server.db_path)
        conn.execute('PRAGMA busy_timeout=5000')
        return conn

    def do_GET(self):
        if not self._allowed_origin():
            return self._error(403, 'Local browser access only')
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == '/api/articles':
            try:
                with closing(self._connect()) as conn:
                    return self._json(query_articles(conn, urllib.parse.parse_qs(parsed.query)))
            except ValueError as exc:
                return self._error(400, str(exc))
        if parsed.path == '/api/article':
            try:
                doi = normalize_doi(urllib.parse.parse_qs(parsed.query).get('doi', [''])[0])
            except ValueError as exc:
                return self._error(400, str(exc))
            with closing(self._connect()) as conn:
                paper = article_by_doi(conn, doi)
            return self._json(paper) if paper else self._error(404, 'Paper not found')
        if parsed.path == '/api/backup':
            return self._backup()
        files = {'/': ('index.html', 'text/html; charset=utf-8'),
                 '/index.html': ('index.html', 'text/html; charset=utf-8'),
                 '/app.js': ('app.js', 'application/javascript; charset=utf-8'),
                 '/styles.css': ('styles.css', 'text/css; charset=utf-8')}
        if parsed.path not in files:
            return self._error(404, 'Not found')
        filename, mime = files[parsed.path]
        data = (WEB_ROOT / filename).read_bytes()
        self._headers(200, mime, len(data))
        self.wfile.write(data)

    def do_PATCH(self):
        if not self._allowed_origin():
            return self._error(403, 'Local browser access only')
        if urllib.parse.urlsplit(self.path).path != '/api/state':
            return self._error(404, 'Not found')
        try:
            body = self._body()
            doi = normalize_doi(body.get('doi'))
            allowed = {'doi', 'acknowledged', 'hidden', 'rating', 'note'}
            if set(body) - allowed or len(body) < 2:
                raise ValueError('Invalid state fields')
            for name in ('acknowledged', 'hidden'):
                if name in body and not isinstance(body[name], bool):
                    raise ValueError(f'{name} must be true or false')
            if 'rating' in body and body['rating'] is not None:
                if type(body['rating']) is not int or not 1 <= body['rating'] <= 5:
                    raise ValueError('Rating must be 1–5 or null')
            if 'note' in body and (not isinstance(body['note'], str) or len(body['note']) > 20000):
                raise ValueError('Note must be text of at most 20,000 characters')
            with closing(self._connect()) as conn:
                set_user_state(conn, doi, acknowledged=body.get('acknowledged'),
                               hidden=body.get('hidden'), rating=body.get('rating', 'unchanged'),
                               note=body.get('note'))
                paper = article_by_doi(conn, doi)
            return self._json({'article': paper})
        except ValueError as exc:
            return self._error(400, str(exc))

    def do_POST(self):
        if not self._allowed_origin():
            return self._error(403, 'Local browser access only')
        path = urllib.parse.urlsplit(self.path).path
        if path not in ('/api/add-doi', '/api/delete'):
            return self._error(404, 'Not found')
        try:
            body = self._body()
            doi = normalize_doi(body.get('doi'))
            if path == '/api/add-doi':
                paper = crossref_doi(doi)
                if paper['doi'] != doi:
                    raise ValueError('Crossref returned a different DOI')
                with closing(self._connect()) as conn:
                    upsert_article(conn, paper, classify(paper), manual=True)
                    result = article_by_doi(conn, doi)
                return self._json({'article': result}, 201)
            if body.get('confirm') is not True:
                raise ValueError('Deletion requires explicit confirmation')
            with closing(self._connect()) as conn:
                if not article_by_doi(conn, doi):
                    return self._error(404, 'Paper not found')
                conn.execute('INSERT OR REPLACE INTO deleted_dois(doi,deleted_at) VALUES(?,?)',
                             (doi, now()))
                conn.execute('DELETE FROM articles WHERE doi=?', (doi,))
                conn.execute('DELETE FROM rejected_candidates WHERE doi=?', (doi,))
                conn.commit()
            return self._json({'deleted': doi, 'blocked_from_auto_reimport': True})
        except ValueError as exc:
            return self._error(400, str(exc))
        except urllib.error.HTTPError as exc:
            return self._error(404 if exc.code == 404 else 502,
                               'DOI not found in Crossref' if exc.code == 404 else 'Crossref request failed')
        except (urllib.error.URLError, TimeoutError):
            return self._error(502, 'Crossref unavailable; check your connection and retry')

    def _backup(self):
        # sqlite3.Connection.backup produces a consistent snapshot including notes
        # and tombstones, unlike copying a live SQLite file with shutil.copyfile.
        with tempfile.TemporaryDirectory(prefix='literature-backup-') as folder:
            target = Path(folder) / 'literature.sqlite3'
            with closing(self._connect()) as source:
                destination = sqlite3.connect(target)
                try:
                    source.backup(destination)
                finally:
                    destination.close()  # Required for Windows temporary-file cleanup.
            filename = 'literature-backup-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.sqlite3'
            self._headers(200, 'application/octet-stream', target.stat().st_size,
                          {'Content-Disposition': f'attachment; filename="{filename}"'})
            with target.open('rb') as stream:
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)


def create_server(db_path='data/literature.sqlite3', host='127.0.0.1', port=8765):
    if host != '127.0.0.1':
        raise ValueError('For safety, the dashboard binds only to 127.0.0.1')
    return LiteratureServer((host, port), db_path)


def serve(db_path='data/literature.sqlite3', port=8765, open_browser=True):
    server = create_server(db_path, port=port)
    url = f'http://127.0.0.1:{server.server_port}/'
    print('Literature dashboard:', url, flush=True)
    print('Database:', Path(db_path).resolve(), flush=True)
    print('Local-only; press Ctrl+C to stop.', flush=True)
    try:
        if open_browser:
            # A brief background launch is not needed; the listening socket is
            # already bound and the browser can wait for the first response.
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
