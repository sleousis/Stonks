import { Component, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';

import { BrandMark } from './brand-mark';
import { countUp, easeOut, motionAllowed } from './count-up';
import { StatTile } from './stat-tile';
import { StatusPill, pillForm } from './status-pill';

describe('BrandMark', () => {
  it('is decorative and draws the rising line, or a falling one when broken', () => {
    const fixture = TestBed.createComponent(BrandMark);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.getAttribute('aria-hidden')).toBe('true');
    expect(el.getAttribute('data-mode')).toBe('still');
    const rising = el.querySelector('path')!.getAttribute('d');
    fixture.componentRef.setInput('mode', 'broken');
    fixture.detectChanges();
    expect(el.getAttribute('data-mode')).toBe('broken');
    expect(el.querySelector('path')!.getAttribute('d')).not.toBe(rising);
  });
});

describe('countUp', () => {
  it('eases out and clamps', () => {
    expect(easeOut(0)).toBe(0);
    expect(easeOut(1)).toBe(1);
    expect(easeOut(2)).toBe(1);
    expect(easeOut(0.5)).toBeGreaterThan(0.5);
  });

  it('jumps straight to the value when the browser cannot animate (tests, reduced motion)', () => {
    expect(motionAllowed()).toBe(false);
    const source = signal<number | null>(null);
    const shown = TestBed.runInInjectionContext(() => countUp(() => source()));
    TestBed.tick();
    expect(shown()).toBeNull();
    source.set(1234.5);
    TestBed.tick();
    expect(shown()).toBe(1234.5);
  });
});

@Component({
  imports: [StatTile],
  template: `<app-stat-tile
    label="Value"
    featured
    [live]="live()"
    [amount]="amount()"
    [format]="fmt"
  />`,
})
class TileHost {
  readonly live = signal(false);
  readonly amount = signal<number | null>(1500);
  readonly fmt = (n: number) => `$${n.toFixed(2)}`;
}

describe('StatTile headline figure', () => {
  it('formats the amount, keeps the final figure for screen readers, and turns brass only when live', () => {
    const fixture = TestBed.createComponent(TileHost);
    fixture.detectChanges();
    const tile = (fixture.nativeElement as HTMLElement).querySelector('app-stat-tile')!;
    expect(tile.querySelector('.value')!.textContent!.trim()).toBe('$1500.00');
    expect(tile.querySelector('.visually-hidden')!.textContent).toContain('$1500.00');
    expect(tile.classList).toContain('featured');
    expect(tile.classList).not.toContain('live');
    fixture.componentInstance.live.set(true);
    fixture.detectChanges();
    expect(tile.classList).toContain('live');
  });
});

describe('StatusPill forms', () => {
  it('gives lifecycle, outcomes, work in progress and alarms their own form', () => {
    expect(pillForm('active', 'positive')).toBe('lamp');
    expect(pillForm('shadow', 'info')).toBe('lamp');
    expect(pillForm('succeeded', 'positive')).toBe('receipt');
    expect(pillForm('failed', 'negative')).toBe('receipt');
    expect(pillForm('running', 'progress')).toBe('working');
    expect(pillForm('halted', 'negative')).toBe('alarm');
    expect(pillForm('something new', 'neutral')).toBe('lamp');
  });

  it('shows the brand mark while working and a mark otherwise', () => {
    const fixture = TestBed.createComponent(StatusPill);
    fixture.componentRef.setInput('status', 'running');
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.getAttribute('data-form')).toBe('working');
    expect(el.querySelector('app-brand-mark')).not.toBeNull();
    fixture.componentRef.setInput('status', 'filled');
    fixture.detectChanges();
    expect(el.getAttribute('data-form')).toBe('receipt');
    expect(el.getAttribute('data-tone')).toBe('positive');
    expect(el.textContent).toContain('filled');
  });
});
