import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { provideApi } from '../../api/provide-api';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { nextRequest, tick } from '../../../testing/http';
import { LOOK_THROUGH } from './insights.fixtures';
import { LookThroughPanel, lookThroughRows } from './look-through';

describe('LookThroughPanel', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        {
          provide: PortfolioContextService,
          useValue: { query: () => ({ portfolio_id: 'pf_1' }) },
        },
      ],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  async function render(portfolio: string | null = 'pf_1') {
    const fixture = TestBed.createComponent(LookThroughPanel);
    fixture.componentRef.setInput('portfolio', portfolio);
    fixture.detectChanges();
    return { fixture, el: fixture.nativeElement as HTMLElement };
  }

  async function settle(fixture: { detectChanges(): void }) {
    await tick(2);
    fixture.detectChanges();
  }

  it('shows your real weight in a name across funds', async () => {
    const { fixture, el } = await render();
    const req = await nextRequest(http, '/api/insights/look-through');
    expect(req.request.urlWithParams).toContain('portfolio_id=pf_1');
    req.flush(LOOK_THROUGH);
    await settle(fixture);
    const text = el.textContent ?? '';
    expect(el.querySelector('h2')?.textContent).toContain('Inside your funds');
    expect(text).toContain('Apple Inc (AAPL.US)');
    expect(text).toContain('23.5%');
    expect(text).toContain('20.0% direct, 3.5% through SPY.US');
    expect(text).toContain('All through SPY.US');
    expect(text).toContain('SPY.US as of');
    expect(text).toContain('10.0% listed');
    expect(text).toContain('leave out');
  });

  it('switches to sectors and names the unlisted rest plainly', async () => {
    const { fixture, el } = await render();
    (await nextRequest(http, '/api/insights/look-through')).flush(LOOK_THROUGH);
    await settle(fixture);
    const sector = [...el.querySelectorAll('button')].find(
      (b) => b.textContent?.trim() === 'Sector',
    )!;
    sector.click();
    fixture.detectChanges();
    const list = el.querySelector('ul[aria-label="Sector"]');
    expect(list?.textContent).toContain('Not in the fund lists');
    expect(list?.textContent).toContain('Cash');
    expect(list?.textContent).toContain('Technology');
  });

  it('waits for a portfolio and offers a retry on failure', async () => {
    const { fixture, el } = await render(null);
    await settle(fixture);
    http.expectNone('/api/insights/look-through');
    fixture.componentRef.setInput('portfolio', 'pf_1');
    fixture.detectChanges();
    (await nextRequest(http, '/api/insights/look-through')).flush(
      { detail: 'down' },
      { status: 500, statusText: 'Server error' },
    );
    await settle(fixture);
    expect(el.textContent).toContain('Could not look inside your funds');
  });

  it('builds rows for each view', () => {
    const rows = lookThroughRows(LOOK_THROUGH.look_through, 'country', 10_000);
    expect(rows).toEqual([
      {
        key: 'US',
        label: 'US',
        value: 2_500,
        weight: 0.25,
        detail: '20.0% direct, 5.0% through funds',
      },
    ]);
  });
});
