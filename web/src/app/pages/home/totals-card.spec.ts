import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { provideApi } from '../../api/provide-api';
import { nextRequest, tick } from '../../../testing/http';
import { TotalsCard } from './totals-card';

describe('TotalsCard', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [...provideApi(), provideHttpClientTesting()] });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  async function render() {
    const fixture = TestBed.createComponent(TotalsCard);
    fixture.detectChanges();
    return { fixture, el: fixture.nativeElement as HTMLElement };
  }

  async function settle(fixture: { detectChanges(): void }) {
    await tick(2);
    fixture.detectChanges();
  }

  it('shows sums across traders and never a holding', async () => {
    const { fixture, el } = await render();
    (await nextRequest(http, '/api/portfolio/totals')).flush({
      cash: 12_000,
      total_value: 250_000,
      portfolios: 4,
      owners: 3,
    });
    await settle(fixture);
    const text = el.textContent ?? '';
    expect(el.querySelector('h2')?.textContent).toContain('All traders');
    expect(text).toContain('$250,000.00');
    expect(text).toContain('$12,000.00');
    expect(text).toMatch(/Portfolios\s*4/);
    expect(text).toMatch(/Traders\s*3/);
    expect(text).toContain("Admins never see anyone's holdings.");
    expect(el.querySelector('.live-frame, .live')).toBeNull();
  });

  it('says when totals are held back instead of showing zeros', async () => {
    const { fixture, el } = await render();
    (await nextRequest(http, '/api/portfolio/totals')).flush({
      cash: 0,
      total_value: 0,
      portfolios: 2,
      owners: 2,
      suppressed: true,
    });
    await settle(fixture);
    const text = el.textContent ?? '';
    expect(text).toContain('Totals appear once three or more traders have live money.');
    expect(text).not.toContain('$0.00');
    expect(el.querySelector('app-stat-tile')).toBeNull();
  });

  it('offers a retry when the totals fail', async () => {
    const { fixture, el } = await render();
    (await nextRequest(http, '/api/portfolio/totals')).flush(
      { detail: 'down' },
      { status: 500, statusText: 'Server error' },
    );
    await settle(fixture);
    expect(el.textContent).toContain('Could not load totals');
    [...el.querySelectorAll('button')].find((b) => b.textContent?.includes('Try again'))!.click();
    (await nextRequest(http, '/api/portfolio/totals')).flush({
      cash: 0,
      total_value: 1,
      portfolios: 1,
      owners: 1,
    });
    await settle(fixture);
    expect(el.textContent).toContain('$1.00');
  });
});
