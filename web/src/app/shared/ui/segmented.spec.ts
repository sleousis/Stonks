import { ChangeDetectionStrategy, Component, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';

import { Segmented, type SegmentOption } from './segmented';

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Segmented],
  template: `<app-segmented label="Group by" [options]="options" [(value)]="value" />`,
})
class Host {
  readonly options: SegmentOption<'a' | 'b' | 'c'>[] = [
    { value: 'a', label: 'Alpha' },
    { value: 'b', label: 'Beta' },
    { value: 'c', label: 'Gamma' },
  ];
  readonly value = signal<'a' | 'b' | 'c'>('a');
}

describe('Segmented', () => {
  function render() {
    const fixture = TestBed.createComponent(Host);
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    const radios = () => [...el.querySelectorAll<HTMLButtonElement>('[role="radio"]')];
    return { fixture, el, radios };
  }

  function key(target: HTMLElement, k: string) {
    target.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true }));
  }

  it('is a labelled radio group with one checked option in the tab order', () => {
    const { el, radios } = render();
    const group = el.querySelector('[role="radiogroup"]');
    expect(group?.getAttribute('aria-label')).toBe('Group by');
    expect(radios().map((r) => r.getAttribute('aria-checked'))).toEqual(['true', 'false', 'false']);
    expect(radios().map((r) => r.tabIndex)).toEqual([0, -1, -1]);
  });

  it('picks an option on click', () => {
    const { fixture, radios } = render();
    radios()[2].click();
    fixture.detectChanges();
    expect(fixture.componentInstance.value()).toBe('c');
    expect(radios()[2].getAttribute('aria-checked')).toBe('true');
    expect(radios()[2].tabIndex).toBe(0);
  });

  it('moves and selects with the arrow keys, wrapping, and Home and End', () => {
    const { fixture, radios } = render();
    const host = fixture.componentInstance;
    radios()[0].focus();

    key(radios()[0], 'ArrowRight');
    fixture.detectChanges();
    expect(host.value()).toBe('b');
    expect(document.activeElement).toBe(radios()[1]);

    key(radios()[1], 'ArrowDown');
    key(radios()[2], 'ArrowRight');
    fixture.detectChanges();
    expect(host.value()).toBe('a');
    expect(document.activeElement).toBe(radios()[0]);

    key(radios()[0], 'ArrowLeft');
    fixture.detectChanges();
    expect(host.value()).toBe('c');

    key(radios()[2], 'Home');
    fixture.detectChanges();
    expect(host.value()).toBe('a');

    key(radios()[0], 'End');
    fixture.detectChanges();
    expect(host.value()).toBe('c');
    expect(radios()[2].getAttribute('aria-checked')).toBe('true');
  });
});
