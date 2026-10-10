import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';

import { routes } from '../../app.routes';

/**
 * Older notifications link to /alerts, /approvals and /subscriptions,
 * which never had a page. They open the page the notify layer links to now
 * (tests/unit/test_notify_deep_links.py checks the links it writes).
 */
describe('Old notification links', () => {
  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideRouter(routes)] });
  });

  const cases: [string, string][] = [
    ['/alerts', '/notifications/price-alerts'],
    ['/approvals', '/tickets'],
    ['/subscriptions', '/'],
  ];

  for (const [link, page] of cases) {
    it(`opens ${page} for ${link}`, async () => {
      const router = TestBed.inject(Router);
      expect(await router.navigateByUrl(link)).toBe(true);
      expect(router.url).toBe(page);
    });
  }
});
