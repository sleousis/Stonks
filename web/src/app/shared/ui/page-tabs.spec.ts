import { Component, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { type PageTab, PageTabs } from './page-tabs';

@Component({
  imports: [PageTabs],
  template: `<app-page-tabs
    idPrefix="t"
    label="Sections"
    [tabs]="tabs()"
    [(selected)]="selected"
  />`,
})
class Host {
  readonly tabs = signal<PageTab[]>([
    { id: 'a', label: 'Alpha' },
    { id: 'b', label: 'Beta' },
    { id: 'c', label: 'Gamma' },
  ]);
  readonly selected = signal<string | null>('a');
}

describe('PageTabs', () => {
  function render() {
    TestBed.configureTestingModule({ providers: [provideRouter([])] });
    const fixture = TestBed.createComponent(Host);
    fixture.detectChanges();
    return { fixture, el: fixture.nativeElement as HTMLElement };
  }

  it('draws section tabs as a tablist with one tab in the tab order (M4)', () => {
    const { el } = render();
    const list = el.querySelector('[role="tablist"]')!;
    expect(list.getAttribute('aria-label')).toBe('Sections');
    const tabs = [...el.querySelectorAll<HTMLButtonElement>('[role="tab"]')];
    expect(tabs.map((t) => t.textContent?.trim())).toEqual(['Alpha', 'Beta', 'Gamma']);
    expect(tabs.map((t) => t.tabIndex)).toEqual([0, -1, -1]);
    expect(tabs[0].getAttribute('aria-selected')).toBe('true');
    expect(tabs[0].id).toBe('t-tab-a');
    expect(tabs[0].getAttribute('aria-controls')).toBe('t-panel-a');
  });

  it('picks on click and moves with the arrow keys, wrapping', () => {
    const { el, fixture } = render();
    const host = fixture.componentInstance;
    const tabs = () => [...el.querySelectorAll<HTMLButtonElement>('[role="tab"]')];
    tabs()[1].click();
    fixture.detectChanges();
    expect(host.selected()).toBe('b');
    tabs()[1].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight' }));
    fixture.detectChanges();
    expect(host.selected()).toBe('c');
    tabs()[2].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight' }));
    fixture.detectChanges();
    expect(host.selected()).toBe('a');
    tabs()[0].dispatchEvent(new KeyboardEvent('keydown', { key: 'End' }));
    fixture.detectChanges();
    expect(host.selected()).toBe('c');
  });

  it('draws page tabs as links in a labelled nav when every tab has a path', () => {
    const { el, fixture } = render();
    fixture.componentInstance.tabs.set([
      { label: 'Orders', path: '/orders' },
      { label: 'Fills', path: '/orders/fills', exact: false },
    ]);
    fixture.detectChanges();
    const nav = el.querySelector('nav')!;
    expect(nav.getAttribute('aria-label')).toBe('Sections');
    expect([...nav.querySelectorAll('a')].map((a) => a.getAttribute('href'))).toEqual([
      '/orders',
      '/orders/fills',
    ]);
    expect(el.querySelector('[role="tablist"]')).toBeNull();
  });
});
