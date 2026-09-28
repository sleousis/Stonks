import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';

import { routes } from '../../app.routes';

describe('Trial results route (UX-09)', () => {
  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideRouter(routes)] });
  });

  it('lives at /paper and old /shadow links land there with their query', async () => {
    const router = TestBed.inject(Router);
    expect(await router.navigateByUrl('/shadow?strategy=mom')).toBe(true);
    expect(router.url).toBe('/paper?strategy=mom');
    expect(await router.navigateByUrl('/paper')).toBe(true);
    expect(router.url).toBe('/paper');
  });
});
