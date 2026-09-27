import type { StrategyStatus } from '../api/models';
import type { SubscriptionMode } from '../api/subscriptions.service';

/**
 * One vocabulary for a strategy's lifecycle, used by Studio and Strategies
 * alike. The API still says shadow, active and retired: traders see paper
 * trading, live and stopped.
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
    label: 'Start paper trading',
    target: 'shadow',
    done: (name) => `Started paper trading for ${name}.`,
  },
  live: {
    label: 'Go live',
    target: 'active',
    done: (name) => `${name} is live. It trades from the next run.`,
  },
  pause: {
    label: 'Back to paper trading',
    target: 'shadow',
    done: (name) => `${name} is back to paper trading.`,
  },
  stop: {
    label: 'Stop',
    target: 'retired',
    done: (name) => `Stopped ${name}.`,
  },
};

/** The stages a strategy moves through, in order. */
export type Stage = 'draft' | 'paper' | 'ready' | 'live';

export const STAGES: readonly { id: Stage; label: string; detail: string }[] = [
  { id: 'draft', label: 'Draft', detail: 'Build and test freely' },
  { id: 'paper', label: 'Paper', detail: 'Decides every run, no real orders' },
  { id: 'ready', label: 'Ready', detail: 'Passed the go-live check' },
  { id: 'live', label: 'Live', detail: 'Places orders from the next run' },
];

/** Trader words for an API status. */
export const STATUS_WORDS: Readonly<Record<StrategyStatus, string>> = {
  shadow: 'Paper trading',
  active: 'Live',
  retired: 'Stopped',
};

/**
 * Server lines (gate details, auto blockers) still name the API's status
 * keys. This puts them in the trader's words (UX-09).
 */
export function toTraderWords(text: string): string {
  return text
    .replace(/'?promotion'? (preset|suite)/gi, 'full test suite')
    .replace(/\bpromotion\b/gi, 'going live')
    .replace(/\bsurvival (reports?|tests?)\b/gi, 'robustness tests')
    .replace(/\bnot active\b/gi, 'not live yet')
    .replace(/\bshadow\b/gi, 'paper trading')
    .replace(/\bretired\b/gi, 'stopped');
}

/**
 * The stage for a registered strategy. `golivePassed` is the go-live
 * verdict when known; a paper strategy that passed it is ready.
 */
export function stageOf(status: StrategyStatus | 'draft', golivePassed?: boolean | null): Stage {
  if (status === 'draft') return 'draft';
  if (status === 'active') return 'live';
  if (status === 'shadow' && golivePassed) return 'ready';
  return 'paper';
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
 * How a trader follows a strategy, in one set of words for Today's switch
 * and the strategy page's Follow panel (UX-31).
 */
export const MODES: readonly ModeOption[] = [
  { value: 'notify', label: 'Signals only', help: 'You get its signals. Nothing trades.' },
  {
    value: 'paper',
    label: 'Paper trading',
    help: 'It trades simulated money in one of your portfolios.',
  },
  {
    value: 'approve',
    label: 'Approve each trade',
    help: 'It proposes real orders. Each waits for your approval before it goes to your broker.',
  },
  { value: 'auto', label: 'Auto', help: 'It places real orders with your broker.' },
];

/** The words for a mode, or the raw value for one this console does not know. */
export function modeLabel(mode: string): string {
  return MODES.find((m) => m.value === mode)?.label ?? mode;
}
