import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { NewsView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { nextRequest, tick } from '../../../testing/http';
import { NewsPanel } from './news-panel';
import { provideFakeDataCoverage } from '../../../testing/fake-data-coverage';

const NEWS: NewsView = {
  tickers: ['AAPL.US'],
  items: [
    {
      ticker: 'AAPL.US',
      published_at: '2026-09-27T10:00:00Z',
      title: 'Apple ships a phone',
      url: 'https://news.example/apple',
      source_name: 'Wire',
      sentiment: 0.6,
      tags: [],
    },
    {
      ticker: 'AAPL.US',
      published_at: '2026-09-26T10:00:00Z',
      title: 'A strange link',
      url: 'javascript:alert(1)',
      sentiment: -0.4,
    },
  ],
  sentiment: [
    { ticker: 'AAPL.US', day: '2026-09-26', sentiment: -0.2, article_count: 1 },
    { ticker: 'AAPL.US', day: '2026-09-27', sentiment: 0.5, article_count: 3 },
  ],
};

describe('NewsPanel', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        provideFakeDataCoverage(),
        provideRouter([]),
        ...provideApi(),
        provideHttpClientTesting(),
      ],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('asks for nothing without a scope and says why', async () => {
    const fixture = TestBed.createComponent(NewsPanel);
    fixture.detectChanges();
    await tick();
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('Pick whose news to show');
    http.expectNone(() => true);
  });

  it('shows the mood per ticker and the newest articles, with safe links only', async () => {
    const fixture = TestBed.createComponent(NewsPanel);
    fixture.componentRef.setInput('query', { scope: 'watchlists', watchlist_id: 'wl_1' });
    fixture.detectChanges();
    const req = await nextRequest(http, '/api/calendars/news');
    expect(req.request.urlWithParams).toContain('watchlist_id=wl_1');
    req.flush(NEWS);
    await tick();
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    const card = el.querySelector('.mood-card')!;
    // (−0.2×1 + 0.5×3) / 4 = 0.325: positive, in words and a shape.
    expect(card.textContent).toContain('Positive');
    expect(card.textContent).toContain('+0.33');
    expect(card.textContent).toContain('4 articles over 2 days');
    const links = [...el.querySelectorAll<HTMLAnchorElement>('a.title')];
    expect(links.map((a) => a.getAttribute('href'))).toEqual(['https://news.example/apple']);
    expect(links[0].rel).toContain('noopener');
    // The unsafe one is plain text.
    expect(el.textContent).toContain('A strange link');
    expect(el.textContent).toContain('Negative');
  });

  it('says when there is no news yet', async () => {
    const fixture = TestBed.createComponent(NewsPanel);
    fixture.componentRef.setInput('query', { scope: 'holdings' });
    fixture.detectChanges();
    (await nextRequest(http, '/api/calendars/news')).flush({
      tickers: [],
      items: [],
      sentiment: [],
    });
    await tick();
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('No news yet');
  });
});
