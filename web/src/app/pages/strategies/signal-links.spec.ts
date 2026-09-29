import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';

import { routes } from '../../app.routes';

describe('Signal notification links', () => {
  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideRouter(routes)] });
  });

  it('opens the strategy for a signal link written before strategy links', async () => {
    const router = TestBed.inject(Router);
    expect(
      await router.navigateByUrl('/signals?strategy=mom_1&ticker=AAA.US&as_of=2026-04-01'),
    ).toBe(true);
    expect(router.url.split('?')[0]).toBe('/strategies/mom_1');
  });

  it('lands on Today when an old link names no strategy', async () => {
    const router = TestBed.inject(Router);
    expect(await router.navigateByUrl('/signals')).toBe(true);
    expect(router.url).toBe('/');
  });
});
