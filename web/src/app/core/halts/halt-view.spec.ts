import type { HaltView } from '../../api/models';
import { haltScopeText, haltSummary } from './halt-view';

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
      title: 'Trading stopped.',
      text: 'Every portfolio. New buys are stopped, sells still go out until someone resumes trading.',
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
      text: 'One portfolio: drawdown breaker. New buys are stopped, sells still go out until it is cleared.',
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
      title: 'Trading stopped.',
      text: 'One portfolio. No new orders go out until someone resumes trading.',
    });
  });

  it('banner never contains pf_ or usr_ (UX-17)', () => {
    const names = new Map([['pf_a', 'Main book']]);
    const scope = (h: Pick<HaltView, 'scope' | 'portfolio_id' | 'user_id'>) =>
      haltScopeText(h, names, 'usr_me');
    const summary = haltSummary(
      [
        halt({ scope: 'portfolio', portfolio_id: 'pf_a' }),
        halt({ id: 2, scope: 'portfolio', portfolio_id: 'pf_unknown' }),
        halt({ id: 3, scope: 'user', user_id: 'usr_me' }),
        halt({ id: 4, scope: 'user', user_id: 'usr_other' }),
      ],
      scope,
    )!;
    expect(summary.text).toContain('Portfolio Main book');
    expect(summary.text).toContain('One portfolio');
    expect(summary.text).toContain('Your portfolios');
    expect(summary.text).toContain("A trader's portfolios");
    expect(summary.text).not.toMatch(/pf_|usr_/);
    // Without names at all, still no ids.
    expect(haltSummary([halt({ scope: 'user', user_id: 'usr_x' })])!.text).not.toMatch(/usr_/);
  });
});

describe('haltScopeText', () => {
  it('says every portfolio, your portfolios or the portfolio name', () => {
    const names = new Map([['pf_default', 'Main']]);
    expect(haltScopeText({ scope: 'global', portfolio_id: null, user_id: null })).toBe(
      'Every portfolio',
    );
    expect(
      haltScopeText({ scope: 'portfolio', portfolio_id: 'pf_default', user_id: null }, names),
    ).toBe('Portfolio Main');
    expect(
      haltScopeText({ scope: 'user', portfolio_id: null, user_id: 'usr_a' }, names, 'usr_a'),
    ).toBe('Your portfolios');
  });
});
