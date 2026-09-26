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

/** How the trader connects: typing keys here, or signing in on the provider's site. */
export function providerFlow(provider: ProviderView): string {
  return provider.auth_flow === 'portal'
    ? `You sign in on the ${provider.display_name} site. Stonks never sees your password.`
    : `You paste API keys from your ${provider.display_name} account.`;
}

/** Brokers with a paper endpoint take an extra `paper` field (Alpaca). */
export function offersPaper(provider: ProviderView): boolean {
  return provider.has_paper;
}

const FIELD_WORDS: Record<string, string> = {
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
  return providers?.find((p) => p.name === name)?.display_name ?? name;
}

function joinWords(words: readonly string[]): string {
  if (words.length === 1) return words[0];
  return `${words.slice(0, -1).join(', ')} and ${words[words.length - 1]}`;
}
