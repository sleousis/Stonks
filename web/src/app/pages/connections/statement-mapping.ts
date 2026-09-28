import type { ColumnMapping } from '../../api/models';

/** The column fields a person maps, in form order. */
export const MAPPED_FIELDS = [
  { key: 'date', label: 'Date', required: true },
  { key: 'type', label: 'Type (buy, sell, dividend...)', required: false },
  { key: 'symbol', label: 'Symbol', required: false },
  { key: 'quantity', label: 'Quantity', required: false },
  { key: 'price', label: 'Price', required: false },
  { key: 'amount', label: 'Amount (cash in or out)', required: false },
  { key: 'fee', label: 'Fee or commission', required: false },
  { key: 'currency', label: 'Currency', required: false },
  { key: 'description', label: 'Notes', required: false },
] as const;

export type MappedField = (typeof MAPPED_FIELDS)[number]['key'];

export const ACTIVITY_KINDS = [
  'trade',
  'dividend',
  'interest',
  'fee',
  'deposit',
  'withdrawal',
  'split',
  'other',
] as const;
export type ActivityKind = (typeof ACTIVITY_KINDS)[number];

/** "BUY=trade, DIV=dividend" from a mapping's type values. */
export function typesToText(types: Record<string, string> | undefined | null): string {
  return Object.entries(types ?? {})
    .map(([k, v]) => `${k}=${v}`)
    .join(', ');
}

/**
 * Type values from "BUY=trade, DIV=dividend". Unknown kinds and blank
 * pairs are dropped and reported, so the form can say which.
 */
export function textToTypes(text: string): { types: Record<string, ActivityKind>; bad: string[] } {
  const types: Record<string, ActivityKind> = {};
  const bad: string[] = [];
  for (const part of text.split(/[,\n]/)) {
    const pair = part.trim();
    if (!pair) continue;
    const [raw, kind] = pair.split('=').map((s) => s.trim());
    const k = (kind ?? '').toLowerCase() as ActivityKind;
    if (!raw || !ACTIVITY_KINDS.includes(k)) {
      bad.push(pair);
      continue;
    }
    types[raw.toUpperCase()] = k;
  }
  return { types, bad };
}

/** "SELL, SOLD" to a list, upper-cased. */
export function textToList(text: string): string[] {
  return text
    .split(/[,\n]/)
    .map((s) => s.trim().toUpperCase())
    .filter(Boolean);
}

/** A mapping with the blank columns left out, ready to send. */
export function cleanMapping(mapping: ColumnMapping): ColumnMapping {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(mapping)) {
    if (v === '' || v === null || v === undefined) continue;
    out[k] = v;
  }
  return out as unknown as ColumnMapping;
}
