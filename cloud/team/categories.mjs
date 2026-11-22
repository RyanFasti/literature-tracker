import {matchesKeywords} from './workspace-config.mjs';
/** Transparent, non-probabilistic topic rules. Suggestions are not validated labels.
 * A manual override (including []) always wins over inferred categories.
 */
export const CATEGORIES = Object.freeze([
  {id:'solar-cell', label:'Solar cell'},
  {id:'photodetector', label:'Photodetector'},
  {id:'led', label:'Light emitter'},
  {id:'film', label:'Film / processing'},
  {id:'stability', label:'Stability'},
  {id:'fundamentals', label:'Materials science'},
  {id:'other-devices', label:'Other devices'},
]);
const RULES = Object.freeze({
  'solar-cell': /\b(?:solar cells?|photovoltaics?|pv devices?|solar modules?)\b/i,
  'photodetector': /\b(?:photodetectors?|photodiodes?|photoconductors?|phototransistors?|light detectors?)\b/i,
  'led': /\b(?:light[ -]emitting diodes?|leds?|electroluminescen\w*|light emitters?)\b/i,
  'film': /\b(?:thin[ -]films?|film formation|spin[ -]coat\w*|blade[ -]coat\w*|slot[ -]die|crystallization|grain boundar\w*|deposition|solution process\w*)\b/i,
  'stability': /\b(?:stabilit\w*|degrad\w*|aging|ageing|durability|decompos\w*|operational lifetime|ion migrat\w*)\b/i,
  'fundamentals': /\b(?:exciton\w*|phonon\w*|bandgap\w*|band gap\w*|defect physics|carrier dynamics|charge transport|crystal structure|phase transition\w*)\b/i,
  'other-devices': /\b(?:transistors?|memristors?|lasers?|sensors?|scintillators?|x[ -]ray detectors?)\b/i,
});
export function suggestCategories(paper, abstract = '') {
  const text = [paper.title||'',abstract||''].join(' ');
  return CATEGORIES.filter(c => RULES[c.id].test(text)).map(c=>c.id);
}
export function categoriesFor(paper, override, abstract = '', customKeywords = null) {
  if (override && override.categories_json !== null && override.categories_json !== undefined) {
    try {
      const list = JSON.parse(override.categories_json);
      if (Array.isArray(list) && list.every(x=>CATEGORIES.some(c=>c.id===x))) return list;
    } catch { /* Invalid legacy row: fall back to suggestions. */ }
  }
  if (customKeywords) {
    const text=[paper.title||'',abstract||''].join(' ');
    return CATEGORIES.filter(c=>matchesKeywords(text,customKeywords[c.id]||[])).map(c=>c.id);
  }
  if (Array.isArray(paper.topics) && paper.topics.every(x=>CATEGORIES.some(c=>c.id===x)))
    return paper.topics;
  return suggestCategories(paper,abstract);
}
export function isManualCategories(override) {
  return Boolean(override && override.categories_json !== null && override.categories_json !== undefined);
}
