import type { BrokerInfo } from '../../api/models';
import {
  brokerLabel,
  isLiveBroker,
  tickConfirmOptions,
  tickRequest,
  tickTicket,
} from './tick-confirm';

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
    expect(opts.confirmLabel).toBe('Start dry run');
  });

  it('makes a real tick name the broker and require typing it', () => {
    const opts = tickConfirmOptions(false, ALPACA_PAPER);
    expect(opts.title).toContain('alpaca paper');
    expect(opts.typedConfirmation).toBe('alpaca paper');
    expect(opts.tone).toBe('danger');
    expect(opts.confirmLabel).toBe('Start trading run');
  });

  it('warns about real money on a live broker', () => {
    expect(tickConfirmOptions(false, ALPACA_LIVE).message).toContain('real money');
    expect(tickConfirmOptions(false, SIMULATED).message).not.toContain('real money');
  });

  it('refuses a real tick without broker information', () => {
    expect(() => tickConfirmOptions(false, null)).toThrow();
  });

  it('shows a real run as a ticket stamped PAPER or LIVE', () => {
    const paper = tickTicket(ALPACA_PAPER, { asOf: '', tickers: '' });
    expect(paper.live).toBe(false);
    expect(paper.typedConfirmation).toBe('alpaca paper');
    expect(paper.confirmLabel).toBe('Start trading run');
    expect(paper.lines.map((l) => [l.label, l.value])).toEqual([
      ['Broker', 'alpaca paper'],
      ['As of', 'Today'],
      ['Tickers', 'All in the universe'],
      ['Strategies', 'Every active strategy'],
    ]);

    const live = tickTicket(ALPACA_LIVE, { asOf: '2026-09-25', tickers: 'aapl.us msft.us' });
    expect(live.live).toBe(true);
    expect(live.message).toContain('real money');
    expect(live.lines.find((l) => l.label === 'Tickers')?.value).toBe('AAPL.US, MSFT.US');
    expect(live.lines.find((l) => l.label === 'As of')).toEqual({
      label: 'As of',
      value: '2026-09-25',
      mono: true,
    });
    expect(tickTicket(SIMULATED, { asOf: '', tickers: '' }).live).toBe(false);
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
