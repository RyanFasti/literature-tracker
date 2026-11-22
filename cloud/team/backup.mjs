/** Private, user-initiated data transfer. No data leaves the browser except to same-origin APIs. */
export const BACKUP_FORMAT = 'literature-tracker-user-state';
export const BACKUP_VERSION = 1;
export const MAX_BACKUP_BYTES = 8 * 1024 * 1024;
const MAX_RECORDS = 20_000;
const MAX_NOTE = 4_000; // Cloudflare Worker limit; do not truncate local notes.

function doi(value) {
  if (typeof value !== 'string') return null;
  const result = value.trim().replace(/^https?:\/\/(?:dx\.)?doi\.org\//i, '').replace(/^doi:\s*/i, '').toLowerCase();
  return result.length <= 255 && /^10\.\d{4,9}\/[^\s?#]+$/i.test(result) ? result : null;
}
function bool(value, name) {
  if (value === true || value === 1) return true;
  if (value === false || value === 0) return false;
  throw new Error(`Invalid ${name}; expected a boolean`);
}
function onlyKeys(item, allowed, label) {
  if (!item || typeof item !== 'object' || Array.isArray(item) ||
      Object.keys(item).some(k => !allowed.includes(k))) throw new Error('Invalid '+label+' fields');
}

export function makeBackup(annotations, manualPapers, exportedAt = new Date().toISOString()) {
  return {
    format: BACKUP_FORMAT, schema_version: BACKUP_VERSION,
    exported_at: exportedAt, source: 'team-dashboard',
    annotations: annotations.map(a => ({
      doi: a.doi, rating: a.rating === undefined ? null : a.rating,
      note: a.note || '', acknowledged: Boolean(a.acknowledged),
      hidden: Boolean(a.hidden), share_note: Boolean(a.share_note),
    })),
    manual_papers: manualPapers.map(p => ({doi: p.doi, title: p.title, journal: p.journal || ''})),
  };
}

/** Validate *all* records before any network mutation; imports are private by default. */
export function validateBackup(raw) {
  onlyKeys(raw, ['format','schema_version','exported_at','source','annotations','manual_papers'], 'backup');
  if (raw.format !== BACKUP_FORMAT || raw.schema_version !== BACKUP_VERSION ||
      !Array.isArray(raw.annotations) || !Array.isArray(raw.manual_papers) ||
      raw.annotations.length > MAX_RECORDS || raw.manual_papers.length > MAX_RECORDS) {
    throw new Error('Unsupported backup format, version or record count');
  }
  const annotations = [], manual_papers = [], seenA = new Set(), seenM = new Set();
  let previouslyShared = 0;
  for (const [index, a] of raw.annotations.entries()) {
    onlyKeys(a, ['doi','rating','note','acknowledged','hidden','share_note','updated_at'], 'annotation '+(index+1));
    const normalized = doi(a.doi);
    if (!normalized || seenA.has(normalized)) throw new Error('Invalid or duplicate annotation DOI at record '+(index+1));
    seenA.add(normalized);
    if (a.rating !== null && !(Number.isInteger(a.rating) && a.rating >= 1 && a.rating <= 5))
      throw new Error('Invalid rating at annotation '+(index+1));
    if (typeof a.note !== 'string' || a.note.length > MAX_NOTE)
      throw new Error('Note at annotation '+(index+1)+' exceeds 4,000 characters or is invalid; edit the local note before importing');
    const shared = bool(a.share_note, 'share_note');
    if (shared) previouslyShared++;
    annotations.push({doi:normalized, rating:a.rating, note:a.note,
      acknowledged:bool(a.acknowledged,'acknowledged'), hidden:bool(a.hidden,'hidden'), share_note:false});
  }
  for (const [index, p] of raw.manual_papers.entries()) {
    onlyKeys(p, ['doi','title','journal','created_at'], 'manual paper '+(index+1));
    const normalized = doi(p.doi);
    if (!normalized || seenM.has(normalized)) throw new Error('Invalid or duplicate manual DOI at record '+(index+1));
    seenM.add(normalized);
    if (typeof p.title !== 'string' || !p.title.trim() || p.title.length > 400 ||
        typeof p.journal !== 'string' || p.journal.length > 150)
      throw new Error('Invalid title or journal at manual paper '+(index+1));
    manual_papers.push({doi:normalized, title:p.title.trim(), journal:p.journal.trim()});
  }
  return {annotations, manual_papers, previouslyShared};
}

/** Existing cloud records are never overwritten, even if a local record is newer. */
export function planImport(validated, existingAnnotations, existingManualPapers) {
  const knownA = new Set(existingAnnotations.map(a => a.doi.toLowerCase()));
  const knownM = new Set(existingManualPapers.map(p => p.doi.toLowerCase()));
  const annotations = validated.annotations.filter(a => !knownA.has(a.doi));
  const manual_papers = validated.manual_papers.filter(p => !knownM.has(p.doi));
  return {annotations, manual_papers,
    skippedAnnotations: validated.annotations.length - annotations.length,
    skippedManual: validated.manual_papers.length - manual_papers.length,
    previouslyShared: validated.previouslyShared};
}
