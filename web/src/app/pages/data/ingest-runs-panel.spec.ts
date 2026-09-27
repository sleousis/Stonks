import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import type { IngestRunView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { nextRequest, tick } from '../../../testing/http';
import { CoveragePanel } from './coverage-panel';
import { IngestRunsPanel } from './ingest-runs-panel';

function runRow(i: number): IngestRunView {
  return {
    id: i,
    source: 'eodhd',
    kind: 'prices',
    started_at: '2026-09-25T20:00:00Z',
    finished_at: '2026-09-25T20:01:00Z',
    tickers_ok: 1,
    tickers_failed: 0,
    status: 'ok',
    error: null,
  };
}

describe('data panels', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [...provideApi(), provideHttpClientTesting()] });
    http = TestBed.inject(HttpTestingController);
  });

  async function settle(fixture: { detectChanges(): void }) {
    for (let i = 0; i < 3; i++) {
      await tick();
      fixture.detectChanges();
    }
  }

  it('ingest history explains itself without command-line steps', async () => {
    const fixture = TestBed.createComponent(IngestRunsPanel);
    fixture.detectChanges();
    (await nextRequest(http, '/api/ingest/runs')).flush({
      items: [],
      total: 0,
      limit: 15,
      offset: 0,
    });
    await settle(fixture);
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('Update data');
    expect(text).not.toMatch(/ingest/i);
    expect(text).not.toMatch(/stonks|command line/);
  });

  it('ingest history keeps the page on screen while the next one loads', async () => {
    const fixture = TestBed.createComponent(IngestRunsPanel);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    (await nextRequest(http, '/api/ingest/runs')).flush({
      items: Array.from({ length: 15 }, (_, i) => runRow(i)),
      total: 45,
      limit: 15,
      offset: 0,
    });
    await settle(fixture);
    [...el.querySelectorAll<HTMLButtonElement>('.pager button')]
      .find((b) => b.textContent?.trim() === 'Next')!
      .click();
    await settle(fixture);
    const req = await nextRequest(http, '/api/ingest/runs');
    expect(req.request.urlWithParams).toContain('offset=15');
    expect(el.querySelector('app-loading-state')).toBeNull();
    expect(el.querySelector('.busy-bar')).not.toBeNull();
    req.flush({
      items: Array.from({ length: 15 }, (_, i) => runRow(15 + i)),
      total: 45,
      limit: 15,
      offset: 15,
    });
    await settle(fixture);
    expect(el.querySelector('.pager .range')?.textContent).toContain('16–30 of 45');
    expect(el.querySelector('.busy-bar')).toBeNull();
  });

  it('coverage says "price data", not "the lake"', async () => {
    const fixture = TestBed.createComponent(CoveragePanel);
    fixture.detectChanges();
    (await nextRequest(http, '/api/market/coverage')).flush({
      items: [],
      total: 0,
      limit: 25,
      offset: 0,
    });
    await settle(fixture);
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('No price data yet');
    expect(text).not.toContain('lake');
  });
});
