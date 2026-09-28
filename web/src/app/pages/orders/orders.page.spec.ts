import { Component } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';

import { tick } from '../../../testing/http';
import { OrdersPage } from './orders.page';
import routes from './orders.routes';

@Component({ template: '' })
class Blank {}

describe('OrdersPage', () => {
  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        provideRouter([
          {
            path: 'orders',
            component: OrdersPage,
            children: [
              { path: '', component: Blank },
              { path: 'fills', component: Blank },
              { path: 'ticks', component: Blank },
            ],
          },
        ]),
      ],
    });
  });

  async function at(url: string): Promise<HTMLElement> {
    const fixture = TestBed.createComponent(OrdersPage);
    await TestBed.inject(Router).navigateByUrl(url);
    await tick();
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  const headerLink = (el: HTMLElement) =>
    el.querySelector<HTMLAnchorElement>('app-page-header a.btn');

  it('has no header link to trading runs: the tabs hold it', async () => {
    const el = await at('/orders');
    expect(headerLink(el)).toBeNull();
    const tab = el.querySelector<HTMLAnchorElement>('nav.tabs a[href="/orders/ticks"]');
    expect(tab?.textContent?.trim()).toBe('Trading runs');
    expect(el.textContent).not.toContain('Run tick');
  });

  it('has no Drafts tab: suggested orders wait in Approvals (F9)', async () => {
    const el = await at('/orders');
    const nav = el.querySelector('nav.tabs')!;
    const tabs = [...nav.querySelectorAll('a')].map((a) => a.textContent?.trim());
    expect(tabs).toEqual([
      'Orders',
      'New order',
      'Rebalance',
      'Fills',
      'Trading runs',
      'Trade costs',
      'Journal',
    ]);
    const drafts = routes[0].children?.find((r) => r.path === 'drafts');
    expect(drafts?.redirectTo).toBe('/tickets');
  });

  it('shows the tabs on the trading runs tab too', async () => {
    const el = await at('/orders/ticks');
    expect(headerLink(el)).toBeNull();
    expect(el.querySelector('nav.tabs')?.textContent).toContain('Trading runs');
  });
});
