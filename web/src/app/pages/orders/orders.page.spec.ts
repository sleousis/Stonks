import { Component } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';

import { tick } from '../../../testing/http';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { OrdersPage } from './orders.page';
import routes from './orders.routes';

@Component({ template: '' })
class Blank {}

describe('OrdersPage', () => {
  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        {
          provide: PortfolioContextService,
          useValue: { state: () => 'ready', noBook: () => false },
        },
        provideRouter([
          {
            path: 'orders',
            component: OrdersPage,
            children: [
              { path: '', component: Blank },
              { path: 'new', component: Blank },
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

  const headerLinks = (el: HTMLElement) =>
    [...el.querySelector('app-page-header')!.querySelectorAll<HTMLAnchorElement>('a.btn')].map(
      (a) => ({
        text: a.textContent?.trim(),
        href: a.getAttribute('href'),
        primary: a.classList.contains('btn-primary'),
      }),
    );

  it('has three tabs that fit a phone, and no Drafts tab (PO-11, F9)', async () => {
    const el = await at('/orders');
    const nav = el.querySelector('nav.tabs')!;
    const tabs = [...nav.querySelectorAll('a')].map((a) => a.textContent?.trim());
    expect(tabs).toEqual(['Orders', 'New order', 'Fills']);
    const drafts = routes[0].children?.find((r) => r.path === 'drafts');
    expect(drafts?.redirectTo).toBe('/tickets');
    // Trading runs and the rebalance planner keep their addresses.
    expect(routes[0].children?.map((r) => r.path)).toEqual(
      expect.arrayContaining(['rebalance', 'ticks', 'ticks/:id']),
    );
  });

  it('keeps New order as a tab only, with Rebalance in the header', async () => {
    const el = await at('/orders');
    expect(headerLinks(el)).toEqual([
      { text: 'Rebalance', href: '/orders/rebalance', primary: false },
    ]);
    expect(el.textContent).not.toContain('Run tick');
  });

  it('offers both CSV downloads on Orders and on Fills, as before the redesign', async () => {
    let el = await at('/orders');
    expect(el.textContent).toContain('Orders CSV');
    expect(el.textContent).toContain('Fills CSV');
    el = await at('/orders/fills');
    expect(el.textContent).toContain('Orders CSV');
    expect(el.textContent).toContain('Fills CSV');
    el = await at('/orders/new');
    expect(el.textContent).not.toContain('CSV');
  });
});
