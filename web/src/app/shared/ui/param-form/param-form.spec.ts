import { ChangeDetectionStrategy, Component, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';

import type { ParameterInfo } from '../../../api/models';
import { ParamForm } from './param-form';
import { type ParamValues, defaultParamValues } from './param-spec';
import { SPEC } from '../../../../testing/lab-fixtures';

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ParamForm],
  template: `<app-param-form
    idPrefix="t"
    [params]="spec()"
    [(values)]="values"
    [showErrors]="show()"
  />`,
})
class Host {
  readonly spec = signal<readonly ParameterInfo[]>(SPEC);
  readonly values = signal<ParamValues>(defaultParamValues(SPEC));
  readonly show = signal(false);
}

function setup() {
  const fixture = TestBed.createComponent(Host);
  fixture.detectChanges();
  return { fixture, host: fixture.componentInstance, el: fixture.nativeElement as HTMLElement };
}

describe('ParamForm', () => {
  it('generates one labelled control per parameter from the spec', () => {
    const { el } = setup();
    const lookback = el.querySelector<HTMLInputElement>('#t-lookback_days')!;
    expect(lookback.type).toBe('number');
    expect(lookback.min).toBe('5');
    expect(lookback.max).toBe('250');
    expect(lookback.step).toBe('1');
    expect(lookback.value).toBe('20');
    expect(el.querySelector('label[for="t-lookback_days"]')!.textContent).toContain(
      'Lookback days',
    );

    expect(el.querySelector<HTMLInputElement>('#t-threshold')!.step).toBe('any');

    const mode = el.querySelector<HTMLSelectElement>('#t-mode')!;
    expect([...mode.options].map((o) => o.textContent)).toEqual(['fast', 'slow']);
    expect(mode.selectedIndex).toBe(0);

    const longOnly = el.querySelector<HTMLInputElement>('#t-long_only')!;
    expect(longOnly.type).toBe('checkbox');
    expect(longOnly.checked).toBe(true);

    expect(el.querySelector<HTMLInputElement>('#t-ticker')!.type).toBe('text');
    expect(el.textContent).toContain('fixed, not tuned');
  });

  it('writes edits back to the two-way values', () => {
    const { fixture, host, el } = setup();
    const lookback = el.querySelector<HTMLInputElement>('#t-lookback_days')!;
    lookback.value = '42';
    lookback.dispatchEvent(new Event('input'));
    const mode = el.querySelector<HTMLSelectElement>('#t-mode')!;
    mode.value = '1';
    mode.dispatchEvent(new Event('change'));
    const longOnly = el.querySelector<HTMLInputElement>('#t-long_only')!;
    longOnly.click();
    fixture.detectChanges();
    expect(host.values()).toMatchObject({ lookback_days: 42, mode: 'slow', long_only: false });
  });

  it('shows errors when asked to', () => {
    const { fixture, host, el } = setup();
    host.values.update((v) => ({ ...v, lookback_days: 900 }));
    fixture.detectChanges();
    expect(el.querySelector('.error')).toBeNull();
    host.show.set(true);
    fixture.detectChanges();
    expect(el.querySelector('.error')!.textContent).toContain('At most 250.');
    expect(el.querySelector('#t-lookback_days')!.getAttribute('aria-invalid')).toBe('true');
  });

  it('says so when a strategy has no parameters', () => {
    const { fixture, host, el } = setup();
    host.spec.set([]);
    fixture.detectChanges();
    expect(el.textContent).toContain('no parameters');
  });
});
