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
    effect: 'A strategy waits some days before it buys a ticker again after a losing exit.',
  },
  stop_guard: {
    label: 'Stop guard',
    effect: 'A strategy buys nothing new after too many losing exits in a short window.',
  },
  losing_lock: {
    label: 'Losing streak lock',
    effect: 'A ticker whose last trades all lost is locked for a while.',
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
      : 'Margin account: buys fit the buying power the broker reports.',
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
