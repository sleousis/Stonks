import type { SegmentOption } from './ui/segmented';

/** Plain words for one rule: a short name and what it does. */
export interface RuleWords {
  label: string;
  effect: string;
}

/**
 * The live safeguards and protections (the risk rules that act only on a
 * book at a real broker), in the order a trading run applies them.
 */
export const SAFEGUARD_WORDS: Record<string, RuleWords> = {
  capital_ramp: {
    label: 'Allocation cap',
    effect: 'Holdings stay within your allocation and what the account is worth.',
  },
  live_notional_caps: {
    label: 'Order size caps',
    effect: 'New orders are cut to the largest size allowed per order and per day.',
  },
  price_band: {
    label: 'Price band',
    effect:
      'Every order gets a limit price near the last price. A new order is dropped if the price moved too far.',
  },
  account_rules: {
    label: 'Account rules',
    effect: 'What the account may do where it is held. See the list below.',
  },
  max_orders_per_run: {
    label: 'Orders per run',
    effect: 'New orders over the limit are dropped. Too many sales stop trading for a check.',
  },
  stop_cooldown: {
    label: 'Cool down after a stop',
    effect:
      'A strategy waits some days before it buys a ticker again after a stop-out. Without protective stops, any losing exit counts.',
  },
  stop_guard: {
    label: 'Stop guard',
    effect:
      'A strategy buys nothing new after too many stop-outs in a short window. Without protective stops, any losing exit counts.',
  },
  losing_lock: {
    label: 'Losing streak lock',
    effect: 'A ticker whose last trades all lost is locked for a while.',
  },
  protective_stops: {
    label: 'Protective stops',
    effect:
      'Each new position gets a stop order at the broker that stays until the position closes. It sells if the price falls too far, even while Stonks is offline.',
  },
};

/** The account rules, by the name the account rules engine gives them. */
export const ACCOUNT_RULE_WORDS: Record<string, RuleWords> = {
  restricted: {
    label: 'Restricted list',
    effect: 'Names on your own list, or that the broker refused before, are never bought.',
  },
  short_permission: {
    label: 'Short selling allowed',
    effect: 'Short sales need a margin account with shorts turned on.',
  },
  account_known: {
    label: 'Account read',
    effect: 'Nothing new is bought while the account balance cannot be read.',
  },
  settled_cash: {
    label: 'Settled cash only',
    effect:
      'Buys use settled cash only. Sale money waits for settlement (next day in the US, two days in the EU and UK).',
  },
  buying_power: {
    label: 'Buying power',
    effect: 'Buys fit the funds the broker says are available.',
  },
  margin_allowed: {
    label: 'Margin allowed',
    effect:
      'New positions on margin need margin accounts turned on by your admin and the broker reporting a margin account.',
  },
  margin_what_if: {
    label: 'Margin check',
    effect:
      'The broker prices each new order first. Its margin must leave a safety share of your equity unused.',
  },
  fx_funding: {
    label: 'Currency on hand',
    effect: 'A buy only spends cash already held in its currency.',
  },
  pdt: {
    label: 'Pattern day trader',
    effect: 'A margin account under 25,000 USD may make only a few day trades a week.',
  },
  wash_sale: {
    label: 'Wash sale',
    effect: 'A buy soon after selling the same ticker at a loss is flagged or blocked.',
  },
  reg_sho: {
    label: 'Short sale rules',
    effect: 'A short sale needs shares to borrow and follows the price test.',
  },
  priips_kid: {
    label: 'Fund documents',
    effect:
      'Retail clients cannot buy a fund without a local key information document. Most US ETFs have none.',
  },
  short_disclosure: {
    label: 'Short disclosure',
    effect: 'Each short stays under 0.1% of the shares issued.',
  },
};

/** Plain words for a rule the words above do not know. */
function fallbackWords(name: string): RuleWords {
  const label = name.replace(/_/g, ' ');
  return { label: label.charAt(0).toUpperCase() + label.slice(1), effect: '' };
}

export function safeguardWords(name: string): RuleWords {
  return SAFEGUARD_WORDS[name] ?? fallbackWords(name);
}

export function accountRuleWords(name: string): RuleWords {
  return ACCOUNT_RULE_WORDS[name] ?? fallbackWords(name);
}

/** The prefix a trading run gives each account rule adjustment. */
const ACCOUNT_RULE_TAG = 'account_rules.';

/**
 * A live rule's risk adjustment in plain words: an account rule
 * (`account_rules.settled_cash`) reads "Account rule: Settled cash only",
 * a live safeguard its own name. Null for any other rule.
 */
export function liveAdjustmentLabel(rule: string): string | null {
  if (rule.startsWith(ACCOUNT_RULE_TAG)) {
    return `Account rule: ${accountRuleWords(rule.slice(ACCOUNT_RULE_TAG.length)).label}`;
  }
  return SAFEGUARD_WORDS[rule]?.label ?? null;
}

export type Jurisdiction = 'us' | 'eu' | 'uk';
export type AccountType = 'cash' | 'margin';
export type ClientClass = 'retail' | 'professional';

export const JURISDICTION_OPTIONS: SegmentOption<Jurisdiction>[] = [
  { value: 'us', label: 'US' },
  { value: 'eu', label: 'EU' },
  { value: 'uk', label: 'UK' },
];

export const ACCOUNT_TYPE_OPTIONS: SegmentOption<AccountType>[] = [
  { value: 'cash', label: 'Cash' },
  { value: 'margin', label: 'Margin' },
];

export const CLIENT_CLASS_OPTIONS: SegmentOption<ClientClass>[] = [
  { value: 'retail', label: 'Retail' },
  { value: 'professional', label: 'Professional' },
];

/** What the chosen profile means for trading, one short line each. */
export function profileNotes(p: {
  jurisdiction: Jurisdiction;
  account_type: AccountType;
  client_class: ClientClass;
}): string[] {
  const notes: string[] = [];
  const settles = p.jurisdiction === 'us' ? 'the next trading day' : 'two trading days later';
  notes.push(
    p.account_type === 'cash'
      ? `Cash account: buys use settled cash only, and sale money settles ${settles}. No short sales.`
      : 'Margin account: each new order fits the margin the broker works out for it, with a safety share left unused. Short sales need shares the broker can lend.',
  );
  if (p.jurisdiction === 'us') {
    notes.push('US rules: wash sales are flagged, and short sales follow the US short sale rules.');
    if (p.account_type === 'margin') {
      notes.push('Under 25,000 USD, only a few day trades a week are allowed.');
    }
  } else {
    notes.push('EU and UK rules: shorts stay under the disclosure level of 0.1% of shares.');
    notes.push(
      p.client_class === 'retail'
        ? 'Retail client: funds without a local key information document cannot be bought.'
        : 'Professional client: the fund document rule does not apply.',
    );
  }
  return notes;
}

/**
 * What a margin account risks, in plain words. Shown before a switch to
 * margin, which needs these acknowledged (roadmap 19.13).
 */
export const MARGIN_RISKS: readonly string[] = [
  'The broker lends you money. You can lose more than you put in.',
  'You pay interest on what you borrow, and a fee for every share you short.',
  'If the account falls below its maintenance margin, the broker can sell your positions without asking, at any price.',
  'A short sale has no ceiling on its loss, and the broker can recall the shares you borrowed.',
  'Stonks sells before the broker does when the cushion gets thin, but a fast market can still beat it.',
];

export type MarginLevel = 'ok' | 'warn' | 'reduce' | 'call';

/** A margin level in plain words, with the tone of its pill. */
export function marginLevelWords(level: MarginLevel | null | undefined): {
  label: string;
  tone: 'positive' | 'warn' | 'negative' | 'neutral';
  effect: string;
} {
  switch (level) {
    case 'ok':
      return {
        label: 'Healthy',
        tone: 'positive',
        effect: 'Plenty of room above the maintenance margin.',
      };
    case 'warn':
      return {
        label: 'Thin',
        tone: 'warn',
        effect: 'The cushion is getting thin. You get an alert. Nothing is sold yet.',
      };
    case 'reduce':
      return {
        label: 'Reducing',
        tone: 'negative',
        effect:
          'Too thin. The next run sells positions until the cushion is back, before the broker does.',
      };
    case 'call':
      return {
        label: 'Margin call',
        tone: 'negative',
        effect:
          'Below the maintenance margin. The broker may be selling now. Add cash or close positions.',
      };
    default:
      return { label: 'No margin', tone: 'neutral', effect: 'A cash account borrows nothing.' };
  }
}
