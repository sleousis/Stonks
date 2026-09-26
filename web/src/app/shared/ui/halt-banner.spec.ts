import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { HaltView } from '../../api/models';
import { HaltStateService } from '../../core/halts/halt-state.service';
import { HaltBanner } from './halt-banner';

describe('HaltBanner', () => {
  const kills = signal<HaltView[]>([]);

  function render() {
    TestBed.configureTestingModule({
      providers: [provideRouter([]), { provide: HaltStateService, useValue: { kills } }],
    });
    const fixture = TestBed.createComponent(HaltBanner);
    fixture.detectChanges();
    return fixture;
  }

  it('shows nothing while no kill switch is on', () => {
    kills.set([]);
    expect(render().nativeElement.querySelector('.banner')).toBeNull();
  });

  it('warns with the scope and what still goes out, and links to the halts page', () => {
    kills.set([{ id: 1, kind: 'kill', scope: 'global', halt: 'buys', active: true } as HaltView]);
    const el = render().nativeElement as HTMLElement;
    const banner = el.querySelector('.banner')!;
    expect(banner.textContent).toContain('Kill switch on.');
    expect(banner.textContent).toContain('Global. New buys are stopped, sells still go out');
    expect(banner.querySelector('a')!.getAttribute('href')).toBe('/ops/halts');
  });

  it('says no orders go out when any switch stops everything', () => {
    kills.set([
      { id: 1, kind: 'kill', scope: 'global', halt: 'buys', active: true } as HaltView,
      {
        id: 2,
        kind: 'kill',
        scope: 'portfolio',
        portfolio_id: 'pf_a',
        halt: 'all',
        active: true,
      } as HaltView,
    ]);
    const text = (render().nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('Global, Portfolio pf_a. No new orders go out');
  });
});
