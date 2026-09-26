import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { FeedItemView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { nextRequest, tick } from '../../../testing/http';
import { SignalsCard, appLink, todaysSignals } from './signals-card';

function item(overrides: Partial<FeedItemView>): FeedItemView {
  return {
    id: 1,
    category: 'signal',
    level: 'info',
    title: 'Buy AAPL.US',
    message: 'momentum-v3 entered AAPL.US',
    deep_link: '/strategies/momentum-v3',
    read_at: null,
    created_at: new Date().toISOString(),
    ...overrides,
  };
}

describe('todaysSignals', () => {
  const now = new Date('2026-09-26T12:00:00Z');

  it('keeps signals from the last 24 hours only', () => {
    const items = [
      item({ id: 1, created_at: '2026-09-26T11:00:00Z' }),
      item({ id: 2, created_at: '2026-09-25T21:00:00Z' }),
      item({ id: 3, created_at: '2026-09-25T11:00:00Z' }),
      item({ id: 4, category: 'order', created_at: '2026-09-26T11:00:00Z' }),
    ];
    expect(todaysSignals(items, now).map((i) => i.id)).toEqual([1, 2]);
  });

  it('opens same-app links only', () => {
    expect(appLink('/strategies/x')).toBe('/strategies/x');
    expect(appLink('https://evil.example')).toBeNull();
    expect(appLink('//evil.example')).toBeNull();
    expect(appLink(null)).toBeNull();
  });
});

describe('SignalsCard', () => {
  let controller: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting()],
    });
    controller = TestBed.inject(HttpTestingController);
  });

  afterEach(() => controller.verify());

  it('lists today signals, marks new ones, and marks them read', async () => {
    const fixture = TestBed.createComponent(SignalsCard);
    fixture.detectChanges();
    const feed = await nextRequest(controller, '/api/notifications');
    expect(feed.request.urlWithParams).toContain('limit=100');
    feed.flush({
      items: [item({ id: 7 }), item({ id: 8, read_at: '2026-09-26T10:00:00Z', title: 'Sell X' })],
      unread_count: 1,
    });
    await tick();
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelectorAll('li')).toHaveLength(2);
    expect(el.querySelectorAll('li.unread')).toHaveLength(1);
    expect(el.querySelector('a.title')?.getAttribute('href')).toBe('/strategies/momentum-v3');

    [...el.querySelectorAll('button')].find((b) => b.textContent?.includes('Mark all'))!.click();
    const read = await nextRequest(controller, '/api/notifications/read', 'POST');
    expect(read.request.body).toEqual({ ids: [7] });
    read.flush({ updated: 1, unread_count: 0 });
    (await nextRequest(controller, '/api/notifications')).flush({ items: [], unread_count: 0 });
    await tick();
    fixture.detectChanges();
    expect(el.textContent).toContain('No signals today');
  });
});
