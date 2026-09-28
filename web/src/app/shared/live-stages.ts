import type { GateCheckView, GateDayView } from '../api/models';

export type LiveStage = 'sim_paper' | 'broker_paper' | 'live_small' | 'live_scale';

/** The stages in order, lowest first. */
export const STAGES: readonly LiveStage[] = [
  'sim_paper',
  'broker_paper',
  'live_small',
  'live_scale',
];

export interface StageWords {
  label: string;
  /** What the stage means, in one line. */
  means: string;
  /** Real money moves at this stage. */
  live: boolean;
}

export const STAGE_WORDS: Record<LiveStage, StageWords> = {
  sim_paper: {
    label: 'Simulated',
    means: 'Stonks fills orders itself. Nothing reaches a broker.',
    live: false,
  },
  broker_paper: {
    label: 'Broker paper',
    means: "Orders go to the broker's paper account. No real money moves.",
    live: false,
  },
  live_small: {
    label: 'Real money, small',
    means: 'Real money, within the allocation you set and tight caps.',
    live: true,
  },
  live_scale: {
    label: 'Real money, full',
    means: 'Real money at your full allocation. Only you raise it, by hand.',
    live: true,
  },
};

export function stageWords(stage: string): StageWords {
  return STAGE_WORDS[stage as LiveStage] ?? { label: stage, means: '', live: false };
}

/** A server sentence with stage ids ("live_small") put in trader words ("Real money, small"). */
export function stageText(text: string): string {
  return text.replace(/\b(sim_paper|broker_paper|live_small|live_scale)\b/g, (id) =>
    stageWords(id).label,
  );
}

/** Plain names of the gate checks, by the name the API gives them (F57). */
const CHECK_LABELS: Record<string, string> = {
  broker_linked: 'Linked to a broker',
  subscriptions: 'Strategies follow it',
  paper_days: 'Paper days done',
  strategies_active: 'Its strategies are approved',
  sessions: 'Trading days at this stage',
  clean_sessions: 'Trouble-free days in a row',
  reconciliation: 'Stonks and the broker agree on what you hold',
  allocation_set: 'Allocation set',
  account_profile_set: 'Account profile saved',
  kill_switch_drill: 'Stop trading tested',
  tracking_error: "Trades like the strategy's test book",
  filled_orders: 'Real-money orders filled',
  tca_gap: 'Costs close to what was expected',
  top_stage: 'Top stage',
};

export function checkLabel(name: string): string {
  return CHECK_LABELS[name] ?? name.replaceAll('_', ' ');
}

export type CheckState = 'pass' | 'fail' | 'none';

/** Pass, fail, or no data yet (not blocking). */
export function checkState(check: Pick<GateCheckView, 'passed'>): CheckState {
  if (check.passed === true) return 'pass';
  if (check.passed === false) return 'fail';
  return 'none';
}

export const CHECK_STATE_WORDS: Record<CheckState, string> = {
  pass: 'Met',
  fail: 'Not met',
  none: 'No data yet',
};

/** Why a session was not clean, in plain words (empty: clean). */
export function dirtyReasons(day: GateDayView, maxRejectRate = 0.02): string[] {
  const out: string[] = [];
  if (day.drift_items) out.push(`${day.drift_items} broker difference(s)`);
  if (day.stuck_orders) out.push(`${day.stuck_orders} order(s) still open`);
  if (day.fills_missing_commission) out.push(`${day.fills_missing_commission} fill(s) with no fee`);
  if (day.orders_sent && day.reject_rate >= maxRejectRate) {
    out.push(`${Math.round(day.reject_rate * 1000) / 10}% rejected`);
  }
  return out;
}

/** The stages a demotion may lead to: every stage below `stage`. */
export function lowerStages(stage: string): LiveStage[] {
  const i = STAGES.indexOf(stage as LiveStage);
  return i > 0 ? STAGES.slice(0, i) : [];
}
