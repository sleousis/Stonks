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

  it('makes a real run name the broker and require typing it', () => {
    const opts = tickConfirmOptions(false, ALPACA_PAPER);
    expect(opts.title).toContain('alpaca paper');
    expect(opts.typedConfirmation).toBe('alpaca paper');
    // B2: a paper run is a paper run in words and colour; red is for real money.
    expect(opts.title).toContain('paper run');
    expect(opts.tone).toBe('default');
    expect(opts.confirmLabel).toBe('Start paper run');
    const live = tickConfirmOptions(false, ALPACA_LIVE);
    expect(live.tone).toBe('danger');
    expect(live.confirmLabel).toBe('Start trading run');
    expect(live.title).toContain('real-money');
  });

  it('warns about real money on a live broker', () => {
    expect(tickConfirmOptions(false, ALPACA_LIVE).message).toContain('real money');
    expect(tickConfirmOptions(false, SIMULATED).message).toContain('No real money moves');
  });

  it('calls a run real money when a portfolio at a real-money stage would act', () => {
    const books: BrokerInfo = { ...SIMULATED, real_money_books: 2 };
    expect(isLiveBroker(books)).toBe(true);
    const opts = tickConfirmOptions(false, books);
    expect(opts.tone).toBe('danger');
    expect(opts.confirmLabel).toBe('Start trading run');
    expect(opts.message).toContain('2 portfolios at a real-money stage');
    expect(opts.message).not.toContain('No real money moves');
    const ticket = tickTicket(books, { asOf: '', tickers: '' });
    expect(ticket.ticket?.live).toBe(true);
    expect(ticket.ticket?.lines.find((l) => l.label === 'Real-money portfolios')?.value).toBe('2');
  });

  it('refuses a real run without broker information', () => {
    expect(() => tickConfirmOptions(false, null)).toThrow();
  });

  it('shows a real run as an order ticket stamped PAPER or LIVE', () => {
    const paper = tickTicket(ALPACA_PAPER, { asOf: '', tickers: '' });
    expect(paper.ticket?.live).toBe(false);
    expect(paper.typedConfirmation).toBe('alpaca paper');
    expect(paper.tone).toBe('default');
    expect(paper.confirmLabel).toBe('Start paper run');
    expect(paper.cancelLabel).toBe('Keep editing');
    expect(paper.ticket?.lines.map((l) => [l.label, l.value])).toEqual([
      ['Broker', 'alpaca paper'],
      ['As of', 'Today'],
      ['Tickers', 'All in the universe'],
      ['Strategies', 'Every approved strategy'],
    ]);

    const live = tickTicket(ALPACA_LIVE, { asOf: '2026-09-25', tickers: 'aapl.us msft.us' });
    expect(live.ticket?.live).toBe(true);
    expect(live.message).toContain('real money');
    expect(live.ticket?.lines.find((l) => l.label === 'Tickers')?.value).toBe('AAPL.US, MSFT.US');
    expect(live.ticket?.lines.find((l) => l.label === 'As of')?.value).toBe('2026-09-25');
    expect(tickTicket(SIMULATED, { asOf: '', tickers: '' }).ticket?.live).toBe(false);
  });

  it('never says tick in what the trader reads', () => {
    for (const opts of [
      tickConfirmOptions(true, null),
      tickConfirmOptions(false, ALPACA_LIVE),
      tickTicket(ALPACA_LIVE, { asOf: '', tickers: '' }),
    ]) {
      expect(`${opts.title} ${opts.message}`).not.toMatch(/\bticks?\b|shadow/i);
    }
    expect(() => tickConfirmOptions(false, null)).toThrow(/trading run/);
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
