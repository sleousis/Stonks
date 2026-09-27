import {
  HttpTestingController,
  type TestRequest,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { Page, ShadowPnlSummary, StrategySummary } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { nextRequest, tick } from '../../../testing/http';
import { STRATEGY_METADATA } from '../../../testing/strategy-fixtures';
import { StrategiesPage } from './strategies.page';

function strategy(id: string, status: StrategySummary['status'], cls: string): StrategySummary {
  return {
    id,
    status,
    class_path: `stonks.strategies.${cls.toLowerCase()}.${cls}`,
    applicable_asset_classes: ['equity'],
    params: {},
    created_at: '2026-09-01T10:00:00Z',
    updated_at: '2026-09-20T10:00:00Z',
    metadata: STRATEGY_METADATA,
  };
}

const ALL: StrategySummary[] = [
  strategy('momentum-v3', 'active', 'MomentumStrategy'),
  strategy('buyhold-spy', 'shadow', 'BuyAndHoldStrategy'),
  strategy('momentum-v1', 'retired', 'MomentumStrategy'),
];

function statusParam(req: TestRequest): string | null {
  return new URL(req.request.urlWithParams, 'http://localhost').searchParams.get('status');
}

function page<T = StrategySummary>(items: T[]): Page<T> {
  return { items, total: items.length, limit: 500, offset: 0 };
}

const PAPER: ShadowPnlSummary[] = [
  {
    strategy_id: 'buyhold-spy',
    status: 'shadow',
    days: 23,
    first_day: '2026-09-01',
    latest_day: '2026-09-24',
    cumulative_return: 0.0412,
    max_drawdown: -0.031,
    total_value: 10412,
  },
];

const isPaper = (req: { url: string }) => req.url.split('?')[0] === '/api/shadow/pnl';

describe('StrategiesPage', () => {
  let fixture: ComponentFixture<StrategiesPage>;
  let controller: HttpTestingController;
  let el: HTMLElement;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [StrategiesPage],
      providers: [...provideApi(), provideHttpClientTesting(), provideRouter([])],
    });
    controller = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(StrategiesPage);
    el = fixture.nativeElement;
    fixture.detectChanges();
  });

  afterEach(() => controller.verify());

  /** Paper summaries answer on their own; `paperFails` makes them fail. */
  let paperFails = false;
  async function settle(): Promise<void> {
    for (let i = 0; i < 4; i++) {
      await tick(5);
      for (const req of controller.match(isPaper)) {
        if (paperFails) {
          req.flush(
            { title: 'Server error', status: 500, detail: 'boom' },
            { status: 500, statusText: 'Server Error' },
          );
        } else {
          req.flush(page(PAPER));
        }
      }
      fixture.detectChanges();
    }
  }

  function rowIds(): string[] {
    return Array.from(el.querySelectorAll('tbody tr td:first-child')).map(
      (td) => td.textContent?.trim() ?? '',
    );
  }

  it('lists strategies with links to their detail pages', async () => {
    const req = await nextRequest(controller, '/api/strategies');
    expect(statusParam(req)).toBeNull();
    req.flush(page(ALL));
    await settle();

    expect(el.querySelector('h1')?.textContent).toContain('Strategies');
    expect(rowIds().sort()).toEqual(['buyhold-spy', 'momentum-v1', 'momentum-v3']);
    const link = el.querySelector('a[href="/strategies/momentum-v3"]');
    expect(link?.textContent).toContain('momentum-v3');
    expect(el.textContent).toContain('3 strategies');
    expect(el.textContent).toContain('Momentum strategy');
    expect(el.textContent).not.toContain('stonks.strategies');
  });

  it('shows paper return, max drawdown and days on paper where there is a paper book', async () => {
    (await nextRequest(controller, '/api/strategies')).flush(page(ALL));
    await settle();

    const headers = Array.from(el.querySelectorAll('thead th')).map((th) => th.textContent ?? '');
    expect(headers.join('|')).toContain('Paper return');
    expect(headers.join('|')).toContain('Max drawdown');
    expect(headers.join('|')).toContain('Days on paper');

    const row = Array.from(el.querySelectorAll('tbody tr')).find((tr) =>
      tr.textContent?.includes('buyhold-spy'),
    )!;
    expect(row.textContent).toContain('+4.12%');
    expect(row.textContent).toContain('-3.10%');
    expect(row.textContent).toContain('23');
  });

  it('keeps the list when the paper results fail', async () => {
    paperFails = true;
    (await nextRequest(controller, '/api/strategies')).flush(page(ALL));
    await settle();
    paperFails = false;
    expect(rowIds().length).toBe(3);
    expect(el.textContent).toContain('Paper results could not be loaded');
  });

  it('does not send traders to the command line when empty', async () => {
    (await nextRequest(controller, '/api/strategies')).flush(page([]));
    await settle();
    expect(el.textContent).toContain('No strategies yet');
    expect(el.textContent).not.toContain('stonks');
    expect(el.textContent).not.toContain('command line');
  });

  it('refetches with the chosen status filter', async () => {
    (await nextRequest(controller, '/api/strategies')).flush(page(ALL));
    await settle();

    const shadow = Array.from(el.querySelectorAll<HTMLLabelElement>('.segment')).find(
      (l) => l.textContent?.trim() === 'Paper trading',
    );
    shadow?.querySelector('input')?.dispatchEvent(new Event('change'));
    fixture.detectChanges();

    const req = await nextRequest(controller, '/api/strategies');
    expect(statusParam(req)).toBe('shadow');
    req.flush(page([ALL[1]]));
    await settle();

    expect(rowIds()).toEqual(['buyhold-spy']);
    expect(shadow?.classList).toContain('selected');
  });

  it('filters by search text in the browser and explains no matches', async () => {
    (await nextRequest(controller, '/api/strategies')).flush(page(ALL));
    await settle();

    const input = el.querySelector<HTMLInputElement>('#strategy-search')!;
    input.value = 'momentum';
    input.dispatchEvent(new Event('input'));
    await settle();
    expect(rowIds().sort()).toEqual(['momentum-v1', 'momentum-v3']);
    expect(el.textContent).toContain('2 of 3 strategies');

    input.value = 'zzz';
    input.dispatchEvent(new Event('input'));
    await settle();
    expect(el.textContent).toContain('No matches');

    const clear = Array.from(el.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Clear search'),
    );
    clear?.click();
    await settle();
    expect(rowIds().length).toBe(3);
  });

  it('explains an empty status', async () => {
    fixture.componentRef.setInput('status', 'retired');
    fixture.detectChanges();
    // The initial unfiltered request may already be out; answer every one.
    await tick(5);
    for (const req of controller.match(() => true)) req.flush(page([]));
    await settle();
    expect(el.textContent).toContain('Nothing is stopped');
    expect(el.textContent).toContain('Show all statuses');
  });

  it('uses trader words only: pills, filters and copy (UX-09)', async () => {
    (await nextRequest(controller, '/api/strategies')).flush(page(ALL));
    await settle();
    const filters = [...el.querySelectorAll('.segment')].map((l) => l.textContent?.trim());
    expect(filters).toEqual(['All', 'Live', 'Paper trading', 'Stopped']);
    const pills = [...el.querySelectorAll('app-status-pill')].map((p) => p.textContent?.trim());
    expect(pills.length).toBeGreaterThan(0);
    for (const pill of pills) expect(['Live', 'Paper trading', 'Stopped']).toContain(pill);
    expect(el.textContent).not.toMatch(/shadow|promot|regist|retire/i);
  });

  it('shows the API message when the list fails', async () => {
    (await nextRequest(controller, '/api/strategies')).flush(
      { title: 'Service Unavailable', status: 503, detail: 'state store is locked' },
      { status: 503, statusText: 'Service Unavailable' },
    );
    await settle();
    expect(el.querySelector('[role="alert"]')?.textContent).toContain('state store is locked');
  });
});
