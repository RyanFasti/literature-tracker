-- Per-user workspace configuration; no global privilege escalation.
CREATE TABLE IF NOT EXISTS workspace_settings (
  github_id INTEGER PRIMARY KEY REFERENCES users(github_id) ON DELETE CASCADE,
  config_json TEXT NOT NULL CHECK (json_valid(config_json)),
  updated_at TEXT NOT NULL
);
-- NULL = automatic article type, values = explicit user correction.
ALTER TABLE paper_overrides ADD COLUMN article_kind TEXT CHECK (article_kind IS NULL OR article_kind IN ('research','review','unknown'));
