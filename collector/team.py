"""Build an allowlisted, public-safe asset directory for the team Worker.

Only public-catalog metadata and pre-reviewed static code are copied. Private
SQLite, local user state, audit archives and credentials are never inputs.
"""
from pathlib import Path
import shutil

from .publication import read_public_catalog

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_FILES = ('index.html', 'app.js', 'styles.css', 'articles.json', '.nojekyll', 'logo.svg', 'logo.png')
TEAM_FILES = ('index.html', 'app.js', 'backup.mjs', 'research.mjs', 'categories.mjs', 'workspace-config.mjs', 'kinds.mjs', 'styles.css')


def build_team_assets(catalog='docs/articles.json', output='cloud/public'):
    source = Path(catalog)
    public = read_public_catalog(source)
    destination = Path(output)
    if source.resolve().is_relative_to(destination.resolve()):
        raise ValueError('Asset output must not contain the source catalog or its parent directory')
    # Never clear an arbitrary user-selected directory. Reject unknown files
    # before writing; only explicitly allowlisted assets are permitted.
    if destination.exists():
        for path in destination.iterdir():
            if path.name in PUBLIC_FILES and path.is_file():
                continue
            if path.name == 'team' and path.is_dir() and all(
                    child.is_file() and child.name in TEAM_FILES for child in path.iterdir()):
                continue
            raise ValueError('Unexpected file in Worker asset directory: ' + str(path))
    destination.mkdir(parents=True, exist_ok=True)
    docs = source.parent
    for name in PUBLIC_FILES:
        if not (docs / name).is_file():
            raise FileNotFoundError('Missing public site asset: ' + str(docs / name))
        shutil.copyfile(docs / name, destination / name)
    team = destination / 'team'
    team.mkdir(exist_ok=True)
    for name in TEAM_FILES:
        shutil.copyfile(ROOT / 'cloud' / 'team' / name, team / name)
    # Read the copied catalog again to fail closed on bad output.
    read_public_catalog(destination / 'articles.json')
    return {'output': str(destination), 'public_articles': len(public['articles']),
            'static_files': len(PUBLIC_FILES) + len(TEAM_FILES),
            'private_annotations_exported': False}
