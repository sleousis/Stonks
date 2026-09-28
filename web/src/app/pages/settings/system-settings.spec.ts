import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';

import {
  type AdminSettingsView,
  AdminSettingsService,
  type SettingField,
} from '../../api/admin-settings.service';
import { settingErrors } from '../../api/admin-settings.service';
import { provideApi } from '../../api/provide-api';
import { ApiError } from '../../core/http/api-error';
import { ToastService } from '../../core/notify/toast.service';
import { nextRequest, tick } from '../../../testing/http';
import { SystemSettings, parseRaw, toRaw, valueText } from './system-settings';

const MAX_WEIGHT: SettingField = {
  key: 'production.risk.max_weight_per_ticker',
  label: 'Most of a portfolio in one ticker',
  help: 'A cap on any single holding.',
  type: 'percent',
  value: 1,
  default: 0.25,
  min: 1,
  max: 100,
  overridden: true,
};
const MIN_DAYS: SettingField = {
  key: 'golive.min_days',
  label: 'Days on trial before approval',
  type: 'int',
  value: 63,
  default: 63,
  min: 20,
  max: 400,
  unit: 'days',
};
const BREAKER: SettingField = {
  key: 'production.risk.rules.circuit_breaker.enabled',
  label: 'Loss limits',
  type: 'bool',
  value: false,
  default: false,
  restart: true,
};
const UNIVERSE: SettingField = {
  key: 'production.universe',
  label: 'Trading universe',
  type: 'choice',
  value: '',
  choices: [
    { value: '', label: 'None' },
    { value: 'sp500', label: 'S&P 500' },
  ],
};

const VIEW: AdminSettingsView = {
  groups: [
    {
      id: 'risk',
      label: 'Risk',
      description: 'Caps every portfolio gets.',
      fields: [MAX_WEIGHT, BREAKER],
    },
    { id: 'trading', label: 'Trading', fields: [UNIVERSE, MIN_DAYS] },
  ],
  updated_at: '2026-09-27T10:00:00Z',
  updated_by: 'Ada Admin',
};

describe('system settings values', () => {
  it('shows percents as whole numbers and sends fractions', () => {
    expect(toRaw(MAX_WEIGHT, 0.25)).toBe('25');
    expect(parseRaw(MAX_WEIGHT, '30')).toEqual({ value: 0.3 });
    expect(parseRaw(MAX_WEIGHT, '0')).toEqual({ error: 'At least 1%.' });
    expect(parseRaw(MAX_WEIGHT, '')).toEqual({ error: 'Enter a number.' });
  });

  it('checks whole numbers and their bounds with the unit', () => {
    expect(parseRaw(MIN_DAYS, '2.5')).toEqual({ error: 'Enter a whole number.' });
    expect(parseRaw(MIN_DAYS, '10')).toEqual({ error: 'At least 20 days.' });
    expect(parseRaw(MIN_DAYS, '90')).toEqual({ value: 90 });
  });

  it('reads lists, switches and choices', () => {
    const list: SettingField = { key: 'x', label: 'X', type: 'list', value: ['A.US'] };
    expect(toRaw(list, ['A.US', 'B.US'])).toBe('A.US, B.US');
    expect(parseRaw(list, 'A.US,  B.US\nC.US')).toEqual({ value: ['A.US', 'B.US', 'C.US'] });
    expect(parseRaw(BREAKER, true)).toEqual({ value: true });
    expect(parseRaw(UNIVERSE, 'nope')).toEqual({ error: 'Pick one of the options.' });
  });

  it('writes defaults in words', () => {
    expect(valueText(MAX_WEIGHT, 0.25)).toBe('25.0%');
    expect(valueText(BREAKER, false)).toBe('Off');
    expect(valueText(UNIVERSE, 'sp500')).toBe('S&P 500');
    expect(valueText(MIN_DAYS, 63)).toBe('63 days');
  });

  it('maps a 422 to the settings it names', () => {
    const err = new ApiError(422, 'Invalid request', 'Invalid', [], null, null, {
      errors: [
        { loc: ['body', 'values', 'golive.min_days'], msg: 'must be at least 20' },
        { key: 'unknown.key', message: 'not allowed' },
      ],
    });
    const { byKey, other } = settingErrors(err, [MIN_DAYS.key, MAX_WEIGHT.key]);
    expect(byKey).toEqual({ 'golive.min_days': 'must be at least 20' });
    expect(other).toBe('not allowed');
  });
});

describe('SystemSettings', () => {
  let fixture: ComponentFixture<SystemSettings>;
  let http: HttpTestingController;
  let el: HTMLElement;

  async function render(body: object, status = 200): Promise<void> {
    TestBed.configureTestingModule({ providers: [...provideApi(), provideHttpClientTesting()] });
    http = TestBed.inject(HttpTestingController);
    // As if the route were in openapi.json (the server that serves it).
    vi.spyOn(TestBed.inject(AdminSettingsService), 'inContract').mockReturnValue(true);
    fixture = TestBed.createComponent(SystemSettings);
    el = fixture.nativeElement;
    fixture.detectChanges();
    const req = await nextRequest(http, '/api/admin/settings');
    if (status === 200) req.flush(body);
    else req.flush(body, { status, statusText: 'Not Found' });
    await settle();
  }

  async function settle(): Promise<void> {
    for (let i = 0; i < 3; i++) {
      await tick();
      fixture.detectChanges();
    }
  }

  function input(id: string): HTMLInputElement {
    return el.querySelector<HTMLInputElement>(`[id="set-${id}"]`)!;
  }

  function type(target: HTMLInputElement, value: string): void {
    target.value = value;
    target.dispatchEvent(new Event('input'));
    fixture.detectChanges();
  }

  function saveButton(): HTMLButtonElement {
    return el.querySelector<HTMLButtonElement>('button[type="submit"]')!;
  }

  afterEach(() => http.verify());

  it('asks nothing and shows nothing while the route is not in the API contract', async () => {
    TestBed.configureTestingModule({ providers: [...provideApi(), provideHttpClientTesting()] });
    http = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(SystemSettings);
    el = fixture.nativeElement;
    fixture.detectChanges();
    await settle();
    http.expectNone('/api/admin/settings');
    expect(el.textContent?.trim()).toBe('');
    expect(fixture.componentInstance.available()).toBe(false);
  });

  it('stays hidden while the server has no editable settings', async () => {
    await render({ title: 'Not Found', status: 404, detail: 'Not Found' }, 404);
    expect(el.textContent?.trim()).toBe('');
    expect(fixture.componentInstance.available()).toBe(false);
  });

  it('shows each group with plain labels, defaults and who saved last', async () => {
    await render(VIEW);
    const text = el.textContent ?? '';
    expect(text).toContain('Operational settings');
    expect(text).toContain('Risk');
    expect(text).toContain('Caps every portfolio gets.');
    expect(text).toContain('Most of a portfolio in one ticker');
    expect(text).toContain('Default: 25.0%.');
    expect(text).toContain('by Ada Admin');
    expect(text).not.toContain('production.risk');
    expect(input(MAX_WEIGHT.key).value).toBe('100');
    // Every control has a label and a hint tied to it.
    expect(el.querySelector(`label[for="set-${MIN_DAYS.key}"]`)).not.toBeNull();
    expect(input(MIN_DAYS.key).getAttribute('aria-describedby')).toContain('-hint');
    expect(saveButton().disabled).toBe(true);
  });

  it('checks values before sending and needs a reason', async () => {
    await render(VIEW);
    type(input(MIN_DAYS.key), '5');
    expect(el.textContent).toContain('At least 20 days.');
    expect(input(MIN_DAYS.key).getAttribute('aria-invalid')).toBe('true');
    saveButton().click();
    fixture.detectChanges();
    expect(el.querySelector('[role="alert"]')!.textContent).toContain('Fix the marked fields');
    expect(el.textContent).toContain('Say why in a few words');
    expect(http.match('/api/admin/settings').filter((r) => r.request.method === 'PUT')).toEqual([]);
  });

  it('saves only the changed values with the reason, then shows what the server kept', async () => {
    await render(VIEW);
    const success = vi.spyOn(TestBed.inject(ToastService), 'success');
    type(input(MAX_WEIGHT.key), '25');
    const breaker = input(BREAKER.key);
    breaker.checked = true;
    breaker.dispatchEvent(new Event('change'));
    type(el.querySelector<HTMLInputElement>('#ops-settings-reason')!, 'Retail defaults');
    expect(saveButton().textContent).toContain('Save 2 changes');
    expect(el.textContent).toContain('take effect after the server restarts');
    saveButton().click();
    const put = await nextRequest(http, '/api/admin/settings', 'PUT');
    expect(put.request.body).toEqual({
      values: {
        'production.risk.max_weight_per_ticker': 0.25,
        'production.risk.rules.circuit_breaker.enabled': true,
      },
      reason: 'Retail defaults',
    });
    put.flush({
      ...VIEW,
      groups: [{ ...VIEW.groups[0], fields: [{ ...MAX_WEIGHT, value: 0.25 }, BREAKER] }],
    });
    await settle();
    expect(success).toHaveBeenCalledWith('Saved 2 settings.');
    expect(input(MAX_WEIGHT.key).value).toBe('25');
    expect(saveButton().disabled).toBe(true);
  });

  it('marks the fields the server refused', async () => {
    await render(VIEW);
    type(input(MIN_DAYS.key), '30');
    type(el.querySelector<HTMLInputElement>('#ops-settings-reason')!, 'Shorter trial');
    saveButton().click();
    const put = await nextRequest(http, '/api/admin/settings', 'PUT');
    put.flush(
      {
        title: 'Invalid request',
        status: 422,
        detail: 'Some values are not allowed.',
        errors: [{ loc: ['body', 'values', 'golive.min_days'], msg: 'must be at least 40' }],
      },
      { status: 422, statusText: 'Unprocessable Entity' },
    );
    await settle();
    expect(el.textContent).toContain('must be at least 40');
    expect(input(MIN_DAYS.key).getAttribute('aria-invalid')).toBe('true');
    expect(el.querySelector('[role="alert"]')).not.toBeNull();
  });
});
