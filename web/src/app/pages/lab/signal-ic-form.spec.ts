import { signal } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';

import { CATALOG, MOMENTUM } from '../../../testing/lab-fixtures';
import type { SignalIcRequest } from '../../api/models';
import { SessionService } from '../../core/auth/session.service';
import { SignalIcFormView } from './signal-ic-form';

const allowed = signal(true);

describe('SignalIcFormView', () => {
  let fixture: ComponentFixture<SignalIcFormView>;
  let el: HTMLElement;
  let emitted: SignalIcRequest[];

  beforeEach(() => {
    emitted = [];
    TestBed.configureTestingModule({ imports: [SignalIcFormView] });
    const session = TestBed.inject(SessionService);
    vi.spyOn(session, 'can').mockImplementation(() => allowed());
    vi.spyOn(session, 'whyNot').mockImplementation(() =>
      allowed() ? null : 'Traders and admins only.',
    );
    fixture = TestBed.createComponent(SignalIcFormView);
    fixture.componentRef.setInput('classes', CATALOG);
    fixture.componentInstance.submitted.subscribe((r) => emitted.push(r));
    el = fixture.nativeElement;
    fixture.detectChanges();
  });

  afterEach(() => allowed.set(true));

  function type(selector: string, value: string): void {
    const node = el.querySelector<HTMLInputElement>(selector)!;
    node.value = value;
    node.dispatchEvent(new Event('input'));
    fixture.detectChanges();
  }

  function fillBasics(): void {
    el.querySelector<HTMLInputElement>(`input[value="${MOMENTUM.class_path}"]`)!.click();
    fixture.detectChanges();
    type('#ic-tickers', 'aapl.us, msft.us');
  }

  function submit(): void {
    el.querySelector<HTMLButtonElement>('button[type="submit"]')!.click();
    fixture.detectChanges();
  }

  it('asks for a strategy and tickers first', () => {
    submit();
    expect(emitted).toEqual([]);
    expect(el.textContent).toContain('Pick a strategy.');
    expect(el.textContent).toContain('Enter at least one ticker.');
  });

  it('sends the strategy, tickers and window, with the default look-aheads when blank', () => {
    fillBasics();
    submit();
    expect(emitted).toHaveLength(1);
    expect(emitted[0]).toMatchObject({
      strategy: { class_path: MOMENTUM.class_path },
      universe: ['AAPL.US', 'MSFT.US'],
      interval: '1d',
    });
    expect(emitted[0].horizons).toBeUndefined();
  });

  it('sends typed look-aheads sorted, and explains a wrong one', () => {
    fillBasics();
    type('#ic-horizons', '21, 1 x');
    submit();
    expect(emitted).toEqual([]);
    expect(el.querySelector('#ic-horizons-hint')?.textContent).toContain('Whole numbers of bars');
    type('#ic-horizons', '21, 1 5');
    submit();
    expect(emitted[0].horizons).toEqual([1, 5, 21]);
  });

  it('keeps Measure off without lab access', () => {
    allowed.set(false);
    fixture.detectChanges();
    fillBasics();
    expect(el.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled).toBe(true);
    submit();
    expect(emitted).toEqual([]);
  });
});
