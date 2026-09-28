import type { StrategyStatus } from '../api/models';
import type { SubscriptionMode } from '../api/subscriptions.service';

/**
 * One vocabulary for a strategy's status, used by Studio, Strategies and
 * every page that names a status (docs/design/vocabulary.md). The API still
 * says shadow, active and retired: people see On trial, Approved and
 * Retired. "Live" is never a strategy word: it only means real money at a
 * broker, which is the portfolio's stage, not the strategy's.
 */
export type LifecycleAction = 'paper' | 'live' | 'pause' | 'stop';

export interface LifecycleWords {
  /** The button label, naming the action. */
  label: string;
  /** The status the API moves the strategy to. */
  target: StrategyStatus;
  /** The success toast, with the same verb. */
  done: (name: string) => string;
}

export const LIFECYCLE: Readonly<Record<LifecycleAction, LifecycleWords>> = {
  paper: {
    label: 'Put on trial',
    target: 'shadow',
    done: (name) => `${name} is on trial. Its test book starts with the next trading run.`,
  },
  live: {
    label: 'Approve',
    target: 'active',
    done: (name) => `${name} is approved. People can follow it.`,
  },
  pause: {
    label: 'Back on trial',
    target: 'shadow',
    done: (name) => `${name} is back on trial.`,
  },
  stop: {
    label: 'Retire',
    target: 'retired',
    done: (name) => `Retired ${name}. It no longer decides.`,
  },
};

/** The steps of a strategy's status ladder, in order. */
export type Stage = 'draft' | 'trial' | 'approved' | 'retired';

export const STAGES: readonly { id: Stage; label: string; detail: string }[] = [
  { id: 'draft', label: 'Draft', detail: 'Being built. Not tested by the system yet.' },
  { id: 'trial', label: 'On trial', detail: 'Paper-tested on its own test book every run' },
  { id: 'approved', label: 'Approved', detail: 'Passed the go-live check. People can follow it.' },
  { id: 'retired', label: 'Retired', detail: 'It no longer decides.' },
];

/** The words people see for an API status. */
export const STATUS_WORDS: Readonly<Record<StrategyStatus, string>> = {
  shadow: 'On trial',
  active: 'Approved',
  retired: 'Retired',
};

/** What each status means, in one plain sentence. */
export const STATUS_MEANING: Readonly<Record<StrategyStatus, string>> = {
  shadow: 'The system paper-tests it on its own test book every run.',
  active: 'Approved: people can follow it.',
  retired: 'It no longer decides.',
};

/**
 * Server lines (gate details, auto blockers) still name the API's status
 * keys. This puts them in the words people read (UX-09).
 */
export function toTraderWords(text: string): string {
  return text
    .replace(/'?promotion'? (preset|suite)/gi, 'full robustness tests')
    .replace(/\bpromotion\b/gi, 'approval')
    .replace(/\bsurvival (reports?|tests?)\b/gi, 'robustness tests')
    .replace(/\bnot active\b/gi, 'not approved yet')
    .replace(/\b(model|shadow|paper) book\b/gi, 'test book')
    .replace(/\b(days?) of paper trading\b/gi, '$1 on trial')
    .replace(/\bpaper trading results\b/gi, 'trial results')
    .replace(/\bpaper trades\b/gi, 'trial trades')
    .replace(/^paper trading, measured\b/i, 'On trial, measured')
    .replace(/\bfull test suite\b/gi, 'full robustness tests')
    .replace(/\bauto mode\b/gi, 'Automatic')
    .replace(/\bin shadow\b/gi, 'on trial')
    .replace(/\bshadow\b/gi, 'on trial');
}

/** The status ladder step for a strategy (a Studio draft is `draft`). */
export function stageOf(status: StrategyStatus | 'draft'): Stage {
  if (status === 'draft') return 'draft';
  if (status === 'active') return 'approved';
  if (status === 'retired') return 'retired';
  return 'trial';
}

/** On trial with a passing go-live check: someone may approve it now. */
export function readyToApprove(
  status: StrategyStatus | 'draft',
  golivePassed: boolean | null | undefined,
): boolean {
  return status === 'shadow' && golivePassed === true;
}

/** Done, current or still to come, for a stage bar. */
export function stageState(stage: Stage, current: Stage): 'done' | 'current' | 'todo' {
  const order = STAGES.map((s) => s.id);
  const i = order.indexOf(stage);
  const c = order.indexOf(current);
  return i < c ? 'done' : i === c ? 'current' : 'todo';
}

export interface ModeOption {
  value: SubscriptionMode;
  label: string;
  /** One line on what the mode does. */
  help: string;
}

/**
 * How a person follows a strategy, in one set of words for Today and the
 * strategy page (UX-31, docs/design/vocabulary.md). Whether a trade uses
 * real money depends on the portfolio's stage, never on the mode.
 */
export const MODES: readonly ModeOption[] = [
  { value: 'notify', label: 'Alerts only', help: 'You get its signals. Nothing trades.' },
  { value: 'paper', label: 'Paper', help: 'It trades your paper portfolio. No real money.' },
  {
    value: 'approve',
    label: 'Approve each trade',
    help: 'Each trade waits for your approval as a ticket.',
  },
  { value: 'auto', label: 'Automatic', help: 'Trades go out without asking.' },
];

/** The words for a mode, or the raw value for one this console does not know. */
export function modeLabel(mode: string): string {
  return MODES.find((m) => m.value === mode)?.label ?? mode;
}
