-- Per-user topic overrides and deletion tombstones. Never modify public catalog.
-- NULL categories_json means automatic classification; [] means manually unclassified.
CREATE TABLE IF NOT EXISTS paper_overrides (
  github_id INTEGER NOT NULL REFERENCES users(github_id) ON DELETE CASCADE,
  doi TEXT NOT NULL,
  categories_json TEXT CHECK (categories_json IS NULL OR json_valid(categories_json)),
  deleted_at TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (github_id, doi)
);
CREATE INDEX IF NOT EXISTS idx_paper_overrides_deleted ON paper_overrides(github_id, deleted_at);
