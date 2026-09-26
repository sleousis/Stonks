import type { HaltView } from '../../api/models';
import { haltSummary } from './halt-view';

function halt(over: Partial<HaltView>): HaltView {
  return {
    id: 1,
    kind: 'kill',
    scope: 'global',
    portfolio_id: null,
    user_id: null,
    halt: 'all',
    active: true,
    ...over,
  } as HaltView;
}

describe('haltSummary', () => {
  it('is null while trading is not halted', () => {
    expect(haltSummary([])).toBeNull();
    expect(haltSummary([halt({ active: false })])).toBeNull();
  });

  it('describes a kill switch with its scope and what still goes out', () => {
    expect(haltSummary([halt({ halt: 'buys' })])).toEqual({
      tone: 'kill',
      title: 'Kill switch on.',
      text: 'Global. New buys are stopped, sells still go out until someone resumes trading.',
    });
  });

  it('describes a tripped circuit breaker', () => {
    expect(
      haltSummary([
        halt({ kind: 'drawdown', scope: 'portfolio', portfolio_id: 'pf_a', halt: 'buys' }),
      ]),
    ).toEqual({
      tone: 'halt',
      title: 'Trading halted.',
      text: 'Portfolio pf_a: drawdown breaker. New buys are stopped, sells still go out until it is cleared.',
    });
  });

  it('puts the kill switch first and says no orders go out when any halt stops all', () => {
    expect(
      haltSummary([
        halt({ kind: 'week_loss', halt: 'buys' }),
        halt({ id: 2, scope: 'portfolio', portfolio_id: 'pf_a', halt: 'all' }),
      ]),
    ).toEqual({
      tone: 'kill',
      title: 'Kill switch on.',
      text: 'Portfolio pf_a. No new orders go out until someone resumes trading.',
    });
  });
});
