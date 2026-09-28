import type {
  CalibrationView,
  ModelVersionView,
  SwapCheckView,
  SwapReportView,
  VersionEventView,
} from '../api/model-versions.service';
import { MISSING, formatDate, formatNumber, formatPercent } from '../core/format/format';
import type { PillForm, PillTone } from './ui/status-pill';

/** A swap override needs a reason of at least this many characters (API rule). */
export const SWAP_OVERRIDE_MIN_REASON = 20;

export interface Look {
  label: string;
  tone: PillTone;
  form: PillForm;
}

/** How each version status looks (docs/model-lifecycle.md). */
export const VERSION_LOOKS: Readonly<Record<string, Look>> = {
  live: { label: 'Live model', tone: 'positive', form: 'lamp' },
  candidate: { label: 'Candidate', tone: 'info', form: 'lamp' },
  archived: { label: 'Archived', tone: 'neutral', form: 'receipt' },
  rejected: { label: 'Rejected', tone: 'negative', form: 'receipt' },
  failed: { label: 'Fit failed', tone: 'negative', form: 'receipt' },
};

export function versionLook(status: string): Look {
  return VERSION_LOOKS[status] ?? { label: status, tone: 'neutral', form: 'receipt' };
}

/** What each log entry means, in a few words. */
const EVENT_WORDS: Readonly<Record<string, string>> = {
  baseline: 'First model recorded',
  candidate: 'New fit saved as a candidate',
  swap: 'Swapped in',
  reject: 'Rejected',
  supersede: 'Replaced by a newer candidate',
  fail: 'Fit failed',
};

export function eventWords(kind: string): string {
  return EVENT_WORDS[kind] ?? kind;
}

/** Plain names for the swap check (src/stonks/lifecycle/check.py). */
const CHECK_LABELS: Readonly<Record<string, string>> = {
  candidate: 'Still a candidate',
  min_days: 'Model book days',
  max_drawdown: 'Model book drawdown',
  vs_live: 'Against the live model',
};

export interface SwapCheckRow {
  name: string;
  label: string;
  passed: boolean;
  value: string;
  limit: string;
  detail: string;
}

export function swapCheckRow(c: SwapCheckView): SwapCheckRow {
  const pct = (v: number | null, signed = false) =>
    v == null ? MISSING : formatPercent(v, { signed });
  let value = MISSING;
  let limit = MISSING;
  if (c.name === 'min_days') {
    value = formatNumber(c.value, { digits: 0 });
    limit = c.limit == null ? MISSING : `≥ ${formatNumber(c.limit, { digits: 0 })}`;
  } else if (c.name === 'max_drawdown') {
    value = pct(c.value);
    limit = c.limit == null ? MISSING : `≤ ${formatPercent(c.limit)}`;
  } else if (c.name === 'vs_live') {
    // A paired test (roadmap 23.9): the HAC t-statistic of the daily gap.
    value = c.value == null ? MISSING : `t ${formatNumber(c.value, { digits: 2 })}`;
    limit = c.limit == null ? MISSING : `≥ ${formatNumber(c.limit, { digits: 2 })}`;
  }
  return {
    name: c.name,
    label: CHECK_LABELS[c.name] ?? c.name,
    passed: c.passed,
    value,
    limit,
    detail: c.detail,
  };
}

/** One version's live calibration, formatted (roadmap 23.9). */
export interface CalibrationRow {
  version: number;
  resolved: string;
  brier: string;
  baseRate: string;
  skill: string;
  ece: string;
  /** Beats always forecasting the base rate (null until resolved). */
  beatsBaseRate: boolean | null;
}

export function calibrationRow(c: CalibrationView): CalibrationRow {
  const num = (v: number | null, digits = 3) => (v == null ? MISSING : formatNumber(v, { digits }));
  return {
    version: c.version,
    resolved: `${formatNumber(c.n_resolved, { digits: 0 })} of ${formatNumber(c.n_forecasts, { digits: 0 })}`,
    brier: num(c.brier),
    baseRate: num(c.brier_base_rate),
    skill: num(c.skill, 2),
    ece: num(c.ece),
    beatsBaseRate: c.skill == null ? null : c.skill > 0,
  };
}

/** "3 Jan 2024 to 2 Jan 2026", or a dash when the fit kept no window. */
export function trainWindow(v: Pick<ModelVersionView, 'train_start' | 'train_end'>): string {
  if (!v.train_start && !v.train_end) return MISSING;
  return `${formatDate(v.train_start)} to ${formatDate(v.train_end)}`;
}

/** The candidate against the live model, over the same days. */
export interface BookComparison {
  days: number;
  candidate: number | null;
  live: number | null;
  /** Candidate minus live, or null while either book has no days. */
  gap: number | null;
  drawdown: number | null;
  /** Bar widths in percent of the larger absolute return (0..100). */
  candidateWidth: number;
  liveWidth: number;
}

export function compareBooks(r: SwapReportView): BookComparison {
  const cand = r.candidate_return;
  const live = r.live_return;
  const scale = Math.max(Math.abs(cand ?? 0), Math.abs(live ?? 0));
  const width = (v: number | null) =>
    v == null || scale === 0 ? 0 : Math.round((Math.abs(v) / scale) * 100);
  return {
    days: r.days,
    candidate: cand,
    live,
    gap: cand != null && live != null ? cand - live : null,
    drawdown: r.candidate_drawdown,
    candidateWidth: width(cand),
    liveWidth: width(live),
  };
}

/** Newest first, for the log. */
export function newestFirst(events: readonly VersionEventView[]): VersionEventView[] {
  return [...events].sort((a, b) => b.created_at.localeCompare(a.created_at) || b.id - a.id);
}
