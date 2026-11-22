import {fetchAbstract} from './abstract.mjs';
import {validateConfig, defaultConfig} from '../team/workspace-config.mjs';
/** Literature Tracker team Worker. No secrets or private state in static assets. */
const SESSION_SECONDS = 30 * 24 * 60 * 60;
const OAUTH_SECONDS = 10 * 60;
const MAX_BODY_BYTES = 65_536;
const MAX_NOTE = 4000;
const PAGE_SIZE = 250;
const SECURITY_HEADERS = {
  'Cache-Control': 'no-store',
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'no-referrer',
  'X-Frame-Options': 'DENY',
};

function json(data, status = 200, extra = {}) {
  return new Response(JSON.stringify(data), {
    status, headers: {'Content-Type': 'application/json; charset=utf-8', ...SECURITY_HEADERS, ...extra},
  });
}
function fail(status, message) { return json({error: message}, status); }
function redirect(location, cookie) {
  return new Response(null, {status: 302, headers: {Location: location, ...SECURITY_HEADERS,
    ...(cookie ? {'Set-Cookie': cookie} : {})}});
}
function cookieValue(request, key) {
  const raw = request.headers.get('Cookie') || '';
  const part = raw.split(';').map(s => s.trim()).find(s => s.startsWith(key + '='));
  return part ? part.slice(key.length + 1) : null;
}
function cookie(name, value, age, path = '/') {
  return `${name}=${value}; Path=${path}; Max-Age=${age}; Secure; HttpOnly; SameSite=Lax`;
}
function randomToken() {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
}
async function tokenHash(token) {
  const hash = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(token));
  return Array.from(new Uint8Array(hash), b => b.toString(16).padStart(2, '0')).join('');
}
function allowlisted(env, login) {
  const names = String(env.ALLOWED_GITHUB_USERS || '').split(',').map(s => s.trim().toLowerCase()).filter(Boolean);
  return names.length > 0 && names.includes(String(login || '').toLowerCase());
}
function configured(env) {
  try {
    const base = new URL(env.PUBLIC_BASE_URL);
    return base.protocol === 'https:' && base.pathname === '/' && !base.search && !base.hash &&
      !base.hostname.includes('REPLACE_WITH') &&
      Boolean(env.GITHUB_CLIENT_ID && env.GITHUB_CLIENT_SECRET && env.DB);
  } catch { return false; }
}
function normalizeDoi(input) {
  if (typeof input !== 'string') return null;
  const doi = input.trim().replace(/^https?:\/\/(?:dx\.)?doi\.org\//i, '').replace(/^doi:\s*/i, '').toLowerCase();
  return doi.length <= 255 && /^10\.\d{4,9}\/[^\s?#]+$/i.test(doi) ? doi : null;
}
function validMutation(request, env) {
  const origin = request.headers.get('Origin');
  const base = new URL(env.PUBLIC_BASE_URL).origin;
  // The custom header forces a cross-origin browser request to preflight;
  // no CORS headers are returned. Origin is additionally checked when present.
  return request.headers.get('X-LT-Request') === '1' &&
    (!origin || origin === base) &&
    /^application\/json(?:\s*;|\s*$)/i.test(request.headers.get('Content-Type') || '');
}
async function bodyJson(request) {
  const size = Number(request.headers.get('Content-Length') || '0');
  if (size > MAX_BODY_BYTES) throw new Error('Request too large');
  const text = await request.text();
  if (new TextEncoder().encode(text).length > MAX_BODY_BYTES) throw new Error('Request too large');
  let value;
  try { value = JSON.parse(text); } catch { throw new Error('Invalid JSON'); }
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Expected JSON object');
  return value;
}
function exactKeys(obj, keys) { return Object.keys(obj).every(k => keys.includes(k)); }
function pageOffset(url) {
  const raw = url.searchParams.get('offset') || '0';
  if (!/^(0|[1-9]\d{0,6})$/.test(raw)) return null;
  return Number(raw);
}
function paged(items, offset) {
  return {items: items.slice(0, PAGE_SIZE), next_offset: items.length > PAGE_SIZE ? offset + PAGE_SIZE : null};
}
async function currentUser(request, env) {
  const token = cookieValue(request, 'lt_session');
  if (!token || !/^[a-f0-9]{64}$/.test(token)) return null;
  const hash = await tokenHash(token);
  const user = await env.DB.prepare(
    'SELECT u.github_id, u.login FROM sessions s JOIN users u ON u.github_id=s.github_id WHERE s.token_hash=? AND s.expires_at>?'
  ).bind(hash, Math.floor(Date.now() / 1000)).first();
  return user && allowlisted(env, user.login) ? user : null;
}
async function oauthLogin(env) {
  if (!configured(env)) return fail(503, 'Team login is not configured');
  const state = randomToken();
  const url = new URL('https://github.com/login/oauth/authorize');
  url.searchParams.set('client_id', env.GITHUB_CLIENT_ID);
  url.searchParams.set('redirect_uri', new URL('/auth/callback', env.PUBLIC_BASE_URL).toString());
  url.searchParams.set('scope', 'read:user');
  url.searchParams.set('state', state);
  return redirect(url.toString(), cookie('lt_oauth_state', state, OAUTH_SECONDS, '/auth'));
}
async function oauthCallback(request, env) {
  const url = new URL(request.url);
  const state = url.searchParams.get('state');
  const code = url.searchParams.get('code');
  const prior = cookieValue(request, 'lt_oauth_state');
  if (!state || !code || !prior || !/^[a-f0-9]{64}$/.test(state) || state !== prior)
    return fail(400, 'Invalid or expired OAuth state. Start sign-in again.');
  if (!configured(env)) return fail(503, 'Team login is not configured');
  const exchange = await fetch('https://github.com/login/oauth/access_token', {
    method: 'POST', headers: {'Accept': 'application/json', 'Content-Type': 'application/x-www-form-urlencoded'},
    body: new URLSearchParams({client_id: env.GITHUB_CLIENT_ID, client_secret: env.GITHUB_CLIENT_SECRET,
      code, redirect_uri: new URL('/auth/callback', env.PUBLIC_BASE_URL).toString()}),
    signal: AbortSignal.timeout(10000),
  });
  if (!exchange.ok) return fail(502, 'GitHub token exchange failed');
  const tokenData = await exchange.json();
  if (typeof tokenData.access_token !== 'string' || !tokenData.access_token)
    return fail(403, 'GitHub authorization was not granted');
  const profile = await fetch('https://api.github.com/user', {
    headers: {'Accept': 'application/vnd.github+json', 'Authorization': `Bearer ${tokenData.access_token}`,
      'User-Agent': 'literature-tracker-team'}, signal: AbortSignal.timeout(10000),
  });
  if (!profile.ok) return fail(502, 'GitHub identity lookup failed');
  const person = await profile.json();
  if (!Number.isSafeInteger(person.id) || person.id <= 0 ||
      typeof person.login !== 'string' || !/^[a-z0-9-]{1,39}$/i.test(person.login))
    return fail(502, 'Invalid GitHub identity');
  if (!allowlisted(env, person.login)) return fail(403, 'This GitHub account is not authorized for this team');
  await env.DB.prepare('INSERT INTO users(github_id,login) VALUES(?,?) ON CONFLICT(github_id) DO UPDATE SET login=excluded.login')
    .bind(person.id, person.login).run();
  const session = randomToken();
  const hash = await tokenHash(session);
  await env.DB.prepare('DELETE FROM sessions WHERE expires_at<=?').bind(Math.floor(Date.now() / 1000)).run();
  await env.DB.prepare('INSERT INTO sessions(token_hash,github_id,expires_at) VALUES(?,?,?)')
    .bind(hash, person.id, Math.floor(Date.now() / 1000) + SESSION_SECONDS).run();
  const response = redirect(new URL('/team/', env.PUBLIC_BASE_URL).toString(), cookie('lt_session', session, SESSION_SECONDS));
  response.headers.append('Set-Cookie', cookie('lt_oauth_state', '', 0, '/auth'));
  return response;
}
async function isDeletedForUser(env, githubId, doi) {
  const row = await env.DB.prepare('SELECT deleted_at FROM paper_overrides WHERE github_id=? AND doi=?')
    .bind(githubId, doi).first();
  return Boolean(row?.deleted_at);
}
async function listRows(env, sql, bindings, offset) {
  const result = await env.DB.prepare(sql).bind(...bindings, PAGE_SIZE + 1, offset).all();
  return paged(result.results || [], offset);
}
async function api(request, env, path, url) {
  const user = await currentUser(request, env);
  if (path === '/api/me' && request.method === 'GET')
    return json(user ? {authenticated: true, login: user.login, github_id: user.github_id} : {authenticated: false});
  if (!user) return fail(401, 'Sign in with an authorized GitHub account');
  if (request.method === 'GET') {
    const offset = pageOffset(url);
    if (offset === null) return fail(400, 'Invalid pagination offset');
    if (path === '/api/workspace-settings') {
      const row = await env.DB.prepare('SELECT config_json,updated_at FROM workspace_settings WHERE github_id=?').bind(user.github_id).first();
      return json({config:row ? JSON.parse(row.config_json) : defaultConfig(),updated_at:row?.updated_at||null});
    }
    if (path === '/api/abstract') {
      const doi = normalizeDoi(url.searchParams.get('doi'));
      if (!doi) return fail(400, 'Invalid DOI');
      // Private, non-persistent on-demand lookup. No abstract enters public assets.
      return json(await fetchAbstract(doi, env));
    }
    if (path === '/api/paper-overrides') return json(await listRows(env,
      'SELECT doi,categories_json,article_kind,deleted_at,updated_at FROM paper_overrides WHERE github_id=? ORDER BY doi LIMIT ? OFFSET ?',
      [user.github_id], offset));
    if (path === '/api/annotations') return json(await listRows(env,
      'SELECT doi,rating,note,acknowledged,hidden,share_note,updated_at FROM annotations WHERE github_id=? ORDER BY doi LIMIT ? OFFSET ?',
      [user.github_id], offset));
    if (path === '/api/shared') {
      // Revoked users' notes remain stored but are not returned to the team.
      const allowed = String(env.ALLOWED_GITHUB_USERS || '').split(',')
        .map(s => s.trim().toLowerCase()).filter(Boolean);
      const placeholders = allowed.map(() => '?').join(',');
      return json(await listRows(env,
        `SELECT a.doi,a.note,a.updated_at,u.login AS author FROM annotations a JOIN users u ON u.github_id=a.github_id WHERE a.share_note=1 AND a.note<>'' AND lower(u.login) IN (${placeholders}) ORDER BY a.updated_at DESC LIMIT ? OFFSET ?`,
        allowed, offset));
    }
    if (path === '/api/manual-papers') return json(await listRows(env,
      'SELECT doi,title,journal,created_at FROM manual_papers WHERE github_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?',
      [user.github_id], offset));
    return fail(404, 'Unknown API route');
  }
  if (!['PUT', 'POST', 'DELETE'].includes(request.method)) return fail(405, 'Method not allowed');
  if (!validMutation(request, env)) return fail(403, 'Request origin or CSRF check failed');
  let data;
  try { data = await bodyJson(request); } catch (e) { return fail(400, e.message); }
  if (path === '/api/workspace-settings' && request.method === 'PUT') {
    if (!exactKeys(data,['config']) || !('config' in data)) return fail(400,'Invalid settings fields');
    try {validateConfig(data.config);} catch(e) {return fail(400,e.message);}
    const stamp=new Date().toISOString();
    await env.DB.prepare(`INSERT INTO workspace_settings(github_id,config_json,updated_at) VALUES(?,?,?)
      ON CONFLICT(github_id) DO UPDATE SET config_json=excluded.config_json,updated_at=excluded.updated_at`)
      .bind(user.github_id,JSON.stringify(data.config),stamp).run();
    return json({ok:true,updated_at:stamp});
  }
  if (path === '/api/article-kind' && request.method === 'PUT') {
    if (!exactKeys(data,['doi','kind']) || !('doi' in data) || !('kind' in data))return fail(400,'Invalid article type fields');
    const doi=normalizeDoi(data.doi);
    if (!doi || (data.kind!==null && !['research','review','unknown'].includes(data.kind)))return fail(400,'Invalid article type');
    if (await isDeletedForUser(env,user.github_id,doi))return fail(409,'Paper was permanently removed from this workspace');
    const stamp=new Date().toISOString();
    await env.DB.prepare(`INSERT INTO paper_overrides(github_id,doi,categories_json,article_kind,deleted_at,updated_at)
      VALUES(?,?,NULL,?,NULL,?) ON CONFLICT(github_id,doi) DO UPDATE SET article_kind=excluded.article_kind,updated_at=excluded.updated_at`)
      .bind(user.github_id,doi,data.kind,stamp).run();
    return json({ok:true,doi,kind:data.kind,updated_at:stamp});
  }
  if (path === '/api/paper-categories' && request.method === 'PUT') {
    if (!exactKeys(data, ['doi','categories']) || !('doi' in data) || !('categories' in data))
      return fail(400, 'Invalid category fields');
    const doi = normalizeDoi(data.doi);
    const categories = data.categories;
    const allowed = new Set(['solar-cell','photodetector','led','film','stability','fundamentals','other-devices']);
    if (!doi || (categories !== null && (!Array.isArray(categories) || categories.length > 7 ||
        new Set(categories).size !== categories.length || categories.some(c => !allowed.has(c)))))
      return fail(400, 'Invalid categories');
    if (await isDeletedForUser(env,user.github_id,doi)) return fail(409,'Paper was permanently removed from this workspace');
    const stamp = new Date().toISOString();
    await env.DB.prepare(`INSERT INTO paper_overrides(github_id,doi,categories_json,deleted_at,updated_at)
      VALUES(?,?,?,NULL,?) ON CONFLICT(github_id,doi) DO UPDATE SET
      categories_json=excluded.categories_json,updated_at=excluded.updated_at`)
      .bind(user.github_id, doi, categories === null ? null : JSON.stringify(categories), stamp).run();
    return json({ok:true,doi,categories,updated_at:stamp});
  }
  if (path === '/api/paper-delete' && request.method === 'POST') {
    if (!exactKeys(data,['doi']) || !('doi' in data)) return fail(400,'Invalid fields');
    const doi = normalizeDoi(data.doi);
    if (!doi) return fail(400,'Invalid DOI');
    const stamp = new Date().toISOString();
    // Tombstone persists across public catalog refreshes. Delete this user's
    // annotations and personal DOI entry, never another user's records.
    await env.DB.batch([
      env.DB.prepare('DELETE FROM annotations WHERE github_id=? AND doi=?').bind(user.github_id,doi),
      env.DB.prepare('DELETE FROM manual_papers WHERE github_id=? AND doi=?').bind(user.github_id,doi),
      env.DB.prepare(`INSERT INTO paper_overrides(github_id,doi,categories_json,deleted_at,updated_at)
        VALUES(?,?,NULL,?,?) ON CONFLICT(github_id,doi) DO UPDATE SET
        categories_json=NULL,deleted_at=excluded.deleted_at,updated_at=excluded.updated_at`)
        .bind(user.github_id,doi,stamp,stamp),
    ]);
    return json({ok:true,doi});
  }
  if ((path === '/api/annotation' && request.method === 'PUT') ||
      (path === '/api/import/annotation' && request.method === 'POST')) {
    const keys = ['doi','rating','note','acknowledged','hidden','share_note'];
    if (!exactKeys(data, keys) || keys.some(k => !(k in data))) return fail(400, 'Invalid annotation fields');
    const doi = normalizeDoi(data.doi);
    if (!doi || (data.rating !== null && (!Number.isInteger(data.rating) || data.rating < 1 || data.rating > 5)) ||
        typeof data.note !== 'string' || data.note.length > MAX_NOTE ||
        typeof data.acknowledged !== 'boolean' || typeof data.hidden !== 'boolean' ||
        typeof data.share_note !== 'boolean') return fail(400, 'Invalid annotation values');
    if (data.share_note && !data.note.trim()) return fail(400, 'Cannot share an empty note');
    if (await isDeletedForUser(env,user.github_id,doi)) return fail(409,'Paper was permanently removed from this workspace');
    const stamp = new Date().toISOString();
    if (path === '/api/import/annotation') {
      // Server-enforced insert-only semantics protect edits made in other tabs/devices.
      // Imported notes are always private regardless of backup contents.
      if (data.share_note) return fail(400, 'Imported notes must be private');
      const inserted = await env.DB.prepare(`INSERT INTO annotations(github_id,doi,rating,note,acknowledged,hidden,share_note,updated_at)
        VALUES(?,?,?,?,?,?,0,?) ON CONFLICT(github_id,doi) DO NOTHING`)
        .bind(user.github_id, doi, data.rating, data.note, Number(data.acknowledged), Number(data.hidden), stamp).run();
      return json({created: Number(inserted.meta?.changes || 0) > 0, doi, updated_at: stamp});
    }
    await env.DB.prepare(`INSERT INTO annotations(github_id,doi,rating,note,acknowledged,hidden,share_note,updated_at)
      VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(github_id,doi) DO UPDATE SET rating=excluded.rating,note=excluded.note,
      acknowledged=excluded.acknowledged,hidden=excluded.hidden,share_note=excluded.share_note,updated_at=excluded.updated_at`)
      .bind(user.github_id, doi, data.rating, data.note, Number(data.acknowledged), Number(data.hidden),
        Number(data.share_note), stamp).run();
    return json({ok: true, doi, updated_at: stamp});
  }
  if (path === '/api/annotation' && request.method === 'DELETE') {
    if (!exactKeys(data, ['doi'])) return fail(400, 'Invalid fields');
    const doi = normalizeDoi(data.doi);
    if (!doi) return fail(400, 'Invalid DOI');
    await env.DB.prepare('DELETE FROM annotations WHERE github_id=? AND doi=?').bind(user.github_id, doi).run();
    return json({ok: true});
  }
  if ((path === '/api/manual-papers' && request.method === 'POST') ||
      (path === '/api/import/manual-paper' && request.method === 'POST')) {
    if (!exactKeys(data, ['doi','title','journal'])) return fail(400, 'Invalid fields');
    const doi = normalizeDoi(data.doi);
    const title = data.title === undefined || data.title === '' ? doi : data.title;
    const journal = data.journal === undefined ? '' : data.journal;
    if (!doi || typeof title !== 'string' || title.length > 400 || !title.trim() ||
        typeof journal !== 'string' || journal.length > 150) return fail(400, 'Invalid manual DOI fields');
    if (await isDeletedForUser(env,user.github_id,doi)) return fail(409,'Paper was permanently removed from this workspace');
    const stamp = new Date().toISOString();
    if (path === '/api/import/manual-paper') {
      const inserted = await env.DB.prepare('INSERT INTO manual_papers(github_id,doi,title,journal,created_at) VALUES(?,?,?,?,?) ON CONFLICT(github_id,doi) DO NOTHING')
        .bind(user.github_id, doi, title.trim(), journal.trim(), stamp).run();
      return json({created: Number(inserted.meta?.changes || 0) > 0,
        paper: {doi, title: title.trim(), journal: journal.trim(), created_at: stamp}});
    }
    await env.DB.prepare('INSERT INTO manual_papers(github_id,doi,title,journal,created_at) VALUES(?,?,?,?,?) ON CONFLICT(github_id,doi) DO UPDATE SET title=excluded.title,journal=excluded.journal')
      .bind(user.github_id, doi, title.trim(), journal.trim(), stamp).run();
    return json({ok: true, paper: {doi, title: title.trim(), journal: journal.trim(), created_at: stamp}});
  }
  if (path === '/api/manual-papers' && request.method === 'DELETE') {
    if (!exactKeys(data, ['doi'])) return fail(400, 'Invalid fields');
    const doi = normalizeDoi(data.doi);
    if (!doi) return fail(400, 'Invalid DOI');
    await env.DB.prepare('DELETE FROM manual_papers WHERE github_id=? AND doi=?').bind(user.github_id, doi).run();
    return json({ok: true});
  }
  return fail(404, 'Unknown API route');
}

export default {
  async fetch(request, env) {
    try {
      const url = new URL(request.url);
      const path = url.pathname;
      if (path.startsWith('/api/') || path.startsWith('/auth/')) {
        if (!env.DB) return fail(503, 'Team database not configured');
        if (path === '/auth/login' && request.method === 'GET') return oauthLogin(env);
        if (path === '/auth/callback' && request.method === 'GET') return oauthCallback(request, env);
        if (path === '/auth/logout' && request.method === 'POST') {
          if (!validMutation(request, env)) return fail(403, 'Request origin or CSRF check failed');
          const token = cookieValue(request, 'lt_session');
          if (token && /^[a-f0-9]{64}$/.test(token))
            await env.DB.prepare('DELETE FROM sessions WHERE token_hash=?').bind(await tokenHash(token)).run();
          return json({ok: true}, 200, {'Set-Cookie': cookie('lt_session', '', 0)});
        }
        if (path.startsWith('/api/')) return api(request, env, path, url);
        return fail(404, 'Unknown authentication route');
      }
      if (request.method !== 'GET' && request.method !== 'HEAD') return fail(405, 'Method not allowed');
      if (path === '/team') return redirect('/team/');
      // The public catalog is deliberately accessible without login; no private
      // data is included in the generated assets. All state lives in D1.
      const response = await env.ASSETS.fetch(request);
      const headers = new Headers(response.headers);
      headers.set('X-Content-Type-Options', 'nosniff');
      headers.set('Referrer-Policy', 'no-referrer');
      headers.set('X-Frame-Options', 'DENY');
      if (path === '/team/' || path === '/team/index.html') {
        headers.set('Cache-Control', 'no-store');
        headers.set('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'");
      }
      return new Response(response.body, {status: response.status, headers});
    } catch (error) {
      console.error('Literature Tracker Worker request failed:', error?.message || 'Unknown error');
      return fail(500, 'Server error. Check Worker logs and database migrations.');
    }
  },
};

export {normalizeDoi, allowlisted, validMutation};
