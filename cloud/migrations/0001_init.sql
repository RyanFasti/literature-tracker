-- Only authenticated, explicitly allowlisted GitHub users can access this D1 database.
-- Notes are private by default; only opt-in shared notes are readable by colleagues.
CREATE TABLE IF NOT EXISTS users (
  github_id INTEGER PRIMARY KEY,
  login TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  github_id INTEGER NOT NULL REFERENCES users(github_id) ON DELETE CASCADE,
  expires_at INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
CREATE TABLE IF NOT EXISTS annotations (
  github_id INTEGER NOT NULL REFERENCES users(github_id) ON DELETE CASCADE,
  doi TEXT NOT NULL,
  rating INTEGER CHECK (rating IS NULL OR rating BETWEEN 1 AND 5),
  note TEXT NOT NULL DEFAULT '',
  acknowledged INTEGER NOT NULL DEFAULT 0 CHECK (acknowledged IN (0,1)),
  hidden INTEGER NOT NULL DEFAULT 0 CHECK (hidden IN (0,1)),
  share_note INTEGER NOT NULL DEFAULT 0 CHECK (share_note IN (0,1)),
  updated_at TEXT NOT NULL,
  PRIMARY KEY (github_id, doi)
);
CREATE INDEX IF NOT EXISTS idx_annotations_shared ON annotations(share_note, updated_at);
CREATE TABLE IF NOT EXISTS manual_papers (
  github_id INTEGER NOT NULL REFERENCES users(github_id) ON DELETE CASCADE,
  doi TEXT NOT NULL,
  title TEXT NOT NULL,
  journal TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  PRIMARY KEY (github_id, doi)
);
