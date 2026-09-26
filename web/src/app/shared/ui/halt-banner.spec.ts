import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { HaltView } from '../../api/models';
import { HaltStateService } from '../../core/halts/halt-state.service';
import { HaltBanner, bannerView } from './halt-banner';

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

describe('HaltBanner', () => {
  const active = signal<HaltView[]>([]);

  function render() {
    TestBed.configureTestingModule({
      providers: [provideRouter([]), { provide: HaltStateService, useValue: { active } }],
    });
    const fixture = TestBed.createComponent(HaltBanner);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('shows nothing while trading is not halted', () => {
    active.set([]);
    expect(render().querySelector('.banner')).toBeNull();
  });

  it('warns about a kill switch with the scope and links to the halts page', () => {
    active.set([halt({ halt: 'buys' })]);
    const banner = render().querySelector('.banner')!;
    expect(banner.getAttribute('data-tone')).toBe('kill');
    expect(banner.textContent).toContain('Kill switch on.');
    expect(banner.textContent).toContain('Global. New buys are stopped, sells still go out');
    expect(banner.querySelector('a')!.getAttribute('href')).toBe('/ops/halts');
  });

  it('shows a tripped circuit breaker too', () => {
    active.set([
      halt({ kind: 'drawdown', scope: 'portfolio', portfolio_id: 'pf_a', halt: 'buys' }),
    ]);
    const banner = render().querySelector('.banner')!;
    expect(banner.getAttribute('data-tone')).toBe('halt');
    expect(banner.textContent).toContain('Trading halted.');
    expect(banner.textContent).toContain('Portfolio pf_a: drawdown breaker. New buys are stopped');
  });

  it('puts the kill switch first and says no orders go out when any halt stops all', () => {
    const view = bannerView([
      halt({ kind: 'week_loss', halt: 'buys' }),
      halt({ id: 2, scope: 'portfolio', portfolio_id: 'pf_a', halt: 'all' }),
    ]);
    expect(view).toEqual({
      tone: 'kill',
      title: 'Kill switch on.',
      text: 'Portfolio pf_a. No new orders go out until someone resumes trading.',
    });
    expect(bannerView([halt({ active: false })])).toBeNull();
  });
});
