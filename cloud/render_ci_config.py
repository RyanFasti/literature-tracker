"""Render a *nonsecret* Wrangler config for GitHub Actions.

Reads public repository variables; does not read or print API tokens or OAuth
secrets. Leaves the user's local cloud/wrangler.toml untouched.
"""
import os
import re
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
DB_UUID = re.compile(r'^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$')
LOGIN = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$')


def render_config(template, variables):
    try:
        data = tomllib.loads(template)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError('Invalid Wrangler TOML template') from exc
    databases = data.get('d1_databases', [])
    if len(databases) != 1 or databases[0].get('binding') != 'DB' or \
            databases[0].get('database_name') != 'literature-tracker-team-db':
        raise ValueError('Expected exactly one D1 binding named DB')
    if data.get('name') != 'literature-tracker-team':
        raise ValueError('Unexpected Worker name')
    public_vars = data.get('vars', {})
    if not all(k in public_vars for k in ('PUBLIC_BASE_URL', 'ALLOWED_GITHUB_USERS')):
        raise ValueError('Missing required public Wrangler variables')
    uuid = variables.get('CLOUDFLARE_D1_DATABASE_ID', '')
    base = variables.get('CLOUDFLARE_TEAM_BASE_URL', '')
    users = variables.get('CLOUDFLARE_ALLOWED_GITHUB_USERS', '')
    if not DB_UUID.fullmatch(uuid):
        raise ValueError('Set CLOUDFLARE_D1_DATABASE_ID to the existing D1 database UUID')
    parsed = urlsplit(base)
    if parsed.scheme != 'https' or not re.fullmatch(
        r'literature-tracker-team\.[a-z0-9-]+\.workers\.dev', parsed.netloc, re.I) or \
            parsed.path not in ('', '/') or parsed.query or parsed.fragment or parsed.username:
        raise ValueError('Set CLOUDFLARE_TEAM_BASE_URL to the existing Worker URL (without /team/)')
    base = base.rstrip('/')
    names = [x.strip() for x in users.split(',')]
    if not 1 <= len(names) <= 25 or not all(LOGIN.fullmatch(x) for x in names) or \
            len(set(x.lower() for x in names)) != len(names):
        raise ValueError('Set CLOUDFLARE_ALLOWED_GITHUB_USERS to comma-separated GitHub usernames')
    if 'ryanfasti' not in {name.lower() for name in names}:
        raise ValueError('The team owner RyanFasti must remain in the deployment allowlist')
    replacements = {
        'database_id': uuid,
        'PUBLIC_BASE_URL': base,
        'ALLOWED_GITHUB_USERS': ','.join(names),
    }
    result = template
    for key, value in replacements.items():
        regex = re.compile(r'(?m)^(' + re.escape(key) + r'\s*=\s*)"[^"]*"\s*$')
        result, count = regex.subn(lambda match: match.group(1) + '"' + value + '"', result)
        if count != 1:
            raise ValueError('Expected exactly one '+key+' assignment in Wrangler template')
    parsed_output = tomllib.loads(result)
    if parsed_output['d1_databases'][0]['database_id'] != uuid or \
            parsed_output['vars']['PUBLIC_BASE_URL'] != base or \
            parsed_output['vars']['ALLOWED_GITHUB_USERS'] != ','.join(names):
        raise ValueError('Generated Wrangler config did not pass validation')
    return result


def main():
    template = (ROOT / 'wrangler.toml').read_text(encoding='utf-8')
    rendered = render_config(template, os.environ)
    output = ROOT / 'wrangler.ci.toml'
    output.write_text(rendered, encoding='utf-8')
    print('Generated nonsecret CI config for literature-tracker-team (D1 binding: DB)')


if __name__ == '__main__':
    main()
