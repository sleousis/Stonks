import { Component } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';

import { tick } from '../../../testing/http';
import { OrdersPage } from './orders.page';

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

  it('offers "Go to trading runs" on the orders tab, as a plain link', async () => {
    const el = await at('/orders');
    const link = headerLink(el);
    expect(link?.textContent?.trim()).toBe('Go to trading runs');
    expect(link?.classList).not.toContain('btn-primary');
    expect(el.textContent).not.toContain('Run tick');
  });

  it('drops the link on the trading runs tab, where the runner is on screen', async () => {
    const el = await at('/orders/ticks');
    expect(headerLink(el)).toBeNull();
    expect(el.querySelector('nav.tabs')?.textContent).toContain('Trading runs');
  });
});
