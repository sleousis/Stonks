import type { ConnectionView, ProviderView, SyncResultView } from '../../api/models';

/** What each read capability gives, in a trader's words. */
const CAPABILITY_WORDS: Record<string, string> = {
  read_positions: 'positions',
  read_balances: 'cash',
  read_activity: 'activity',
  read_orders: 'orders',
};

/** "Reads positions, cash and activity." from the provider's capabilities. */
export function providerGives(provider: ProviderView): string {
  // In a fixed order, whatever order the server lists them in.
  const words = Object.entries(CAPABILITY_WORDS)
    .filter(([capability]) => provider.capabilities.includes(capability))
    .map(([, word]) => word);
  if (words.length === 0) return 'Reads your account.';
  return `Reads ${joinWords(words)}.`;
}

/**
 * Whether Stonks ever places orders through this broker (F53): a provider
 * that can trade places orders only when you allow it, one that cannot
 * only reads.
 */
export function providerReach(provider: Pick<ProviderView, 'can_trade'>): string {
  return provider.can_trade
    ? 'Reads, and places orders when you allow it.'
    : 'Reads only. It never places an order there.';
}

/** How the trader connects: typing keys here, or signing in on the provider's site. */
export function providerFlow(provider: ProviderView): string {
  if (provider.needs_gateway) {
    return 'Runs through an IB Gateway on the server. Pick the gateway your admin set up.';
  }
  return provider.auth_flow === 'portal'
    ? `You sign in on the ${provider.display_name} site. Stonks never sees your password.`
    : `You paste API keys from your ${provider.display_name} account.`;
}

/** Where to find the keys, for providers where that is not obvious. */
export const KEY_HINTS: Record<string, string> = {
  etoro:
    'In eToro open Settings, then Trading, then API keys. Create a key pair for your demo or real account.',
};

/** Brokers with a paper endpoint take an extra `paper` field (Alpaca). */
export function offersPaper(provider: ProviderView): boolean {
  return provider.has_paper;
}

const FIELD_WORDS: Record<string, string> = {
  gateway: 'Gateway',
  api_key: 'API key',
  secret_key: 'Secret key',
  api_secret: 'API secret',
  token: 'Token',
};

/** `secret_key` -> "Secret key". */
export function fieldLabel(field: string): string {
  if (FIELD_WORDS[field]) return FIELD_WORDS[field];
  const words = field.replace(/[_-]+/g, ' ').trim().toLowerCase();
  const text = words.replace(/\bapi\b/g, 'API').replace(/\bid\b/g, 'ID');
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** The field name inside a sentence: "secret key", but "API key" keeps its capitals. */
export function fieldNoun(field: string): string {
  const label = fieldLabel(field);
  return /^[A-Z][a-z]/.test(label) ? label.charAt(0).toLowerCase() + label.slice(1) : label;
}

/** The CSV statements connection (23.17) is not a provider: name it here. */
export const CSV_PROVIDER = 'csv';

export const CONNECTION_STATUS_LABEL: Record<ConnectionView['status'], string> = {
  pending: 'Waiting to finish',
  active: 'Connected',
  error: 'Needs attention',
};

export const SYNC_STATUS_LABEL: Record<SyncResultView['status'], string> = {
  ok: 'Synced',
  partial: 'Partly synced',
  error: 'Sync failed',
};

/** The provider's display name, or its short name when the list is not loaded. */
export function providerName(providers: readonly ProviderView[] | undefined, name: string): string {
  if (name === CSV_PROVIDER) return 'CSV imports';
  return providers?.find((p) => p.name === name)?.display_name ?? name;
}

/** "Alpaca, IBKR and SnapTrade". */
export function joinNames(names: readonly string[]): string {
  return joinWords(names);
}

function joinWords(words: readonly string[]): string {
  if (words.length === 1) return words[0];
  return `${words.slice(0, -1).join(', ')} and ${words[words.length - 1]}`;
}
