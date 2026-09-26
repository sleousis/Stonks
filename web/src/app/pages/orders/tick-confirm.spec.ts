import type { BrokerInfo } from '../../api/models';
import { brokerLabel, isLiveBroker, tickConfirmOptions, tickRequest } from './tick-confirm';

const SIMULATED: BrokerInfo = {
  kind: 'simulated',
  paper: true,
  allow_live: false,
  credentials_configured: false,
};
const ALPACA_PAPER: BrokerInfo = { ...SIMULATED, kind: 'alpaca', credentials_configured: true };
const ALPACA_LIVE: BrokerInfo = { ...ALPACA_PAPER, paper: false, allow_live: true };

describe('tick confirmation', () => {
  it('labels brokers by kind and account type', () => {
    expect(brokerLabel(SIMULATED)).toBe('simulated');
    expect(brokerLabel(ALPACA_PAPER)).toBe('alpaca paper');
    expect(brokerLabel(ALPACA_LIVE)).toBe('alpaca live');
    expect(isLiveBroker(ALPACA_LIVE)).toBe(true);
    expect(isLiveBroker(ALPACA_PAPER)).toBe(false);
  });

  it('asks a dry run with a plain click, no typed confirmation', () => {
    const opts = tickConfirmOptions(true, null);
    expect(opts.typedConfirmation).toBeUndefined();
    expect(opts.tone).toBeUndefined();
    expect(opts.confirmLabel).toBe('Run dry run');
  });

  it('makes a real tick name the broker and require typing it', () => {
    const opts = tickConfirmOptions(false, ALPACA_PAPER);
    expect(opts.title).toContain('alpaca paper');
    expect(opts.typedConfirmation).toBe('alpaca paper');
    expect(opts.tone).toBe('danger');
    expect(opts.confirmLabel).toBe('Run tick');
  });

  it('warns about real money on a live broker', () => {
    expect(tickConfirmOptions(false, ALPACA_LIVE).message).toContain('real money');
    expect(tickConfirmOptions(false, SIMULATED).message).not.toContain('real money');
  });

  it('refuses a real tick without broker information', () => {
    expect(() => tickConfirmOptions(false, null)).toThrow();
  });

  it('builds the request body from the form', () => {
    expect(tickRequest({ dryRun: true, asOf: '', tickers: '' })).toEqual({
      dry_run: true,
      as_of: null,
      tickers: null,
    });
    expect(
      tickRequest({ dryRun: false, asOf: '2026-09-25', tickers: 'aapl.us, msft.us  ' }),
    ).toEqual({ dry_run: false, as_of: '2026-09-25', tickers: ['AAPL.US', 'MSFT.US'] });
  });
});
