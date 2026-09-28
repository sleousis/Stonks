import type { TickSummary } from '../../api/models';
import { humanize, tickNotes, tickOutcome } from './tick-summary';

describe('tickOutcome', () => {
  it('names the winner', () => {
    expect(tickOutcome({ winner_strategy_id: 'momentum-v3' })).toEqual({
      kind: 'winner',
      text: 'momentum-v3',
      strategyId: 'momentum-v3',
      strategyName: 'momentum-v3',
    });
  });

  it("names the winner by its title, never the starter's id", () => {
    const outcome = tickOutcome({
      winner_strategy_id: 'starter_trend',
      winner_strategy_name: 'Starter: trend following',
    });
    expect(outcome.text).toBe('Starter: trend following');
    expect(outcome.strategyId).toBe('starter_trend');
    expect(tickOutcome({ winner_strategy_id: 'momentum_0a1b2c3d' }).text).toBe('Momentum 0a1b');
  });

  it('names the exit strategy when no candidate qualified', () => {
    const summary: TickSummary = {
      winner_strategy_id: null,
      reason: 'no_candidates',
      exit_strategy_id: 'value-v1',
    };
    expect(tickOutcome(summary)).toEqual({
      kind: 'exit',
      text: 'Exit value-v1',
      strategyId: 'value-v1',
      strategyName: 'value-v1',
    });
  });

  it('reports errors and empty summaries', () => {
    expect(tickOutcome({ error: 'boom', error_type: 'ValueError' }).kind).toBe('error');
    expect(tickOutcome(null).text).toBe('–');
    expect(tickOutcome({ reason: 'no_candidates' }).text).toBe('No candidates');
  });
});

describe('tickNotes', () => {
  it('lists stale buys and open-order conflicts', () => {
    expect(
      tickNotes({ stale_buys_dropped: ['AAPL.US'], open_order_conflicts: ['MSFT.US'] }),
    ).toEqual([
      'Dropped buys on stale prices: AAPL.US.',
      'Skipped tickers with open orders at the broker: MSFT.US.',
    ]);
    expect(tickNotes({ stale_buys_dropped: [] })).toEqual([]);
  });

  it('humanizes snake case', () => {
    expect(humanize('max_position_weight')).toBe('Max position weight');
  });
});
