import { fieldLabel, offersPaper, providerGives, providerName } from './connection-labels';
import { ALPACA, SNAPTRADE } from './connections.fixtures';

describe('connection labels', () => {
  it('names key fields in plain words', () => {
    expect(fieldLabel('api_key')).toBe('API key');
    expect(fieldLabel('secret_key')).toBe('Secret key');
    expect(fieldLabel('client_id')).toBe('Client ID');
    expect(fieldLabel('user-secret')).toBe('User secret');
  });

  it('says what a provider reads, in a fixed order', () => {
    expect(providerGives(ALPACA)).toBe('Reads positions, cash and activity.');
    expect(providerGives({ ...ALPACA, capabilities: ['read_balances'] })).toBe('Reads cash.');
    expect(providerGives({ ...ALPACA, capabilities: [] })).toBe('Reads your account.');
  });

  it('offers a paper switch only for key brokers that can trade', () => {
    expect(offersPaper(ALPACA)).toBe(true);
    expect(offersPaper(SNAPTRADE)).toBe(false);
  });

  it('falls back to the short name when the provider list is missing', () => {
    expect(providerName([ALPACA], 'alpaca')).toBe('Alpaca');
    expect(providerName(undefined, 'alpaca')).toBe('alpaca');
  });
});
