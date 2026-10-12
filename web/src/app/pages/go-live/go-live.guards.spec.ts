import { TestBed } from '@angular/core/testing';
import {
  type ActivatedRouteSnapshot,
  convertToParamMap,
  type RouterStateSnapshot,
  Router,
  UrlTree,
  provideRouter,
} from '@angular/router';

import { oldReviewLinkGuard } from './go-live.guards';

describe('oldReviewLinkGuard', () => {
  function run(query: Record<string, string>): unknown {
    TestBed.configureTestingModule({ providers: [provideRouter([])] });
    const route = { queryParamMap: convertToParamMap(query) } as ActivatedRouteSnapshot;
    return TestBed.runInInjectionContext(() =>
      oldReviewLinkGuard(route, { url: '/go-live' } as RouterStateSnapshot),
    );
  }

  it('lets a plain visit through to the admin check', () => {
    expect(run({})).toBe(true);
  });

  it('sends an old ?strategy= link to that Review tab, for anyone', () => {
    const result = run({ strategy: 'value_1a2b3c4d' });
    expect(result).toBeInstanceOf(UrlTree);
    expect(TestBed.inject(Router).serializeUrl(result as UrlTree)).toBe(
      '/strategies/value_1a2b3c4d?tab=review',
    );
  });
});
