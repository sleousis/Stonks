import { signal } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';

import { CATALOG, MOMENTUM } from '../../../testing/lab-fixtures';
import type { SweepRequest, UniverseView } from '../../api/models';
import { SessionService } from '../../core/auth/session.service';
import { SweepFormView } from './sweep-form';

const allowed = signal(true);

describe('SweepFormView', () => {
  let fixture: ComponentFixture<SweepFormView>;
  let el: HTMLElement;
  let emitted: SweepRequest[];

  beforeEach(() => {
    emitted = [];
    TestBed.configureTestingModule({ imports: [SweepFormView] });
    const session = TestBed.inject(SessionService);
    vi.spyOn(session, 'can').mockImplementation(() => allowed());
    vi.spyOn(session, 'whyNot').mockImplementation(() =>
      allowed() ? null : 'Traders and admins only.',
    );
    fixture = TestBed.createComponent(SweepFormView);
    fixture.componentRef.setInput('classes', CATALOG);
    fixture.componentInstance.submitted.subscribe((r) => emitted.push(r));
    el = fixture.nativeElement;
    fixture.detectChanges();
  });

  afterEach(() => allowed.set(true));

  function type(selector: string, value: string, event = 'input'): void {
    const node = el.querySelector<HTMLInputElement>(selector)!;
    node.value = value;
    node.dispatchEvent(new Event(event));
    fixture.detectChanges();
  }

  function submit(): void {
    el.querySelector<HTMLButtonElement>('button[type="submit"]')!.click();
    fixture.detectChanges();
  }

  it('says what to fix and sends nothing when the basket is empty', () => {
    submit();
    expect(emitted).toEqual([]);
    expect(el.textContent).toContain('Enter at least one ticker.');
  });

  it('sends every strategy on the typed tickers with the quick suite by default', () => {
    type('#sw-tickers', 'spy.us qqq.us');
    submit();
    expect(emitted).toHaveLength(1);
    expect(emitted[0]).toMatchObject({ universe: ['SPY.US', 'QQQ.US'], preset: 'quick' });
    expect(emitted[0].strategies).toBeUndefined();
    expect(el.textContent).toContain(`all ${CATALOG.length}`);
  });

  it('offers the named suites in trader words and sends the picked strategies', () => {
    const suite = el.querySelector<HTMLSelectElement>('#sw-suite')!;
    expect([...suite.options].map((o) => o.textContent!.trim())).toEqual([
      'Quick',
      'Standard',
      'Go-live',
    ]);
    type('#sw-suite', 'promotion', 'change');
    const box = [...el.querySelectorAll<HTMLLabelElement>('details label.check')]
      .find((l) => l.textContent!.includes(MOMENTUM.name))!
      .querySelector('input')!;
    box.click();
    fixture.detectChanges();
    expect(el.textContent).toContain('1 picked');
    type('#sw-tickers', 'SPY.US');
    submit();
    expect(emitted[0]).toMatchObject({ preset: 'promotion', strategies: [MOMENTUM.class_path] });
  });

  it('runs on a saved universe', () => {
    const universe: UniverseView = { id: 'sp500', kind: 'index', name: 'S&P 500', spec: {} };
    fixture.componentRef.setInput('universes', [universe]);
    fixture.detectChanges();
    const radio = [...el.querySelectorAll<HTMLLabelElement>('label')]
      .find((l) => l.textContent!.includes('A saved universe'))!
      .querySelector('input')!;
    radio.click();
    fixture.detectChanges();
    submit();
    expect(emitted).toEqual([]);
    expect(el.textContent).toContain('Pick a universe.');
    type('#sw-universe', 'sp500', 'change');
    submit();
    expect(emitted[0]).toMatchObject({ universe_id: 'sp500' });
    expect(emitted[0].universe).toBeUndefined();
  });

  it('keeps Start off with a reason without lab access', () => {
    allowed.set(false);
    fixture.detectChanges();
    type('#sw-tickers', 'SPY.US');
    const start = el.querySelector<HTMLButtonElement>('button[type="submit"]')!;
    expect(start.disabled).toBe(true);
    expect(el.textContent).toContain('Traders and admins only.');
    submit();
    expect(emitted).toEqual([]);
  });
});
