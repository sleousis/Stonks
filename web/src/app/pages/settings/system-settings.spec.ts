import type { MockInstance } from 'vitest';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';

import type { SystemSettingView } from '../../api/generated/types.gen';
import { provideApi } from '../../api/provide-api';
import { StepUpService } from '../../core/auth/step-up.service';
import { ApiError } from '../../core/http/api-error';
import { ToastService } from '../../core/notify/toast.service';
import { nextRequest, tick } from '../../../testing/http';
import {
  KEEP_CURRENT,
  SystemSettings,
  keyError,
  kindOf,
  parseRaw,
  settingLabel,
  toRaw,
  valueText,
} from './system-settings';

function item(over: Partial<SystemSettingView>): SystemSettingView {
  return {
    key: 'x',
    group: 'risk',
    label: 'X',
    help: 'Help.',
    applies: 'next_run',
    value: null,
    default: null,
    overridden: false,
    choices: null,
    ...over,
  };
}

const MAX_WEIGHT = item({
  key: 'production.risk.max_weight_per_ticker',
  label: 'Largest holding',
  help: 'Most of a portfolio one ticker may take.',
  value: 0.25,
  default: 0.25,
});
const MAX_POSITIONS = item({
  key: 'production.risk.max_open_positions',
  label: 'Open positions',
  help: 'Most tickers held at once. Empty means no limit.',
  value: null,
  default: null,
});
const ENABLED = item({
  key: 'production.risk.enabled',
  label: 'Risk limits on',
  value: true,
  default: true,
});
const UNIVERSE = item({
  key: 'production.universe',
  group: 'trading',
  label: 'Trading universe',
  value: ['AAA.US', 'BBB.US'],
  default: [],
  choices: ['starter', 'sp500'],
});
const MODEL_BOOKS = item({
  key: 'production.model_books',
  group: 'trading',
  label: 'Test books',
  value: 'all',
  default: 'all',
  choices: ['all', 'shadow'],
  overridden: true,
  updated_at: '2026-09-27T10:00:00Z',
  updated_by: 'Ada Admin',
  reason: 'Keep every record',
});
const SCHEDULE = item({
  key: 'production.risk.rules.drawdown_scaling.schedule',
  label: 'Size down in a drawdown',
  value: [[0.1, 0.5]],
  default: [[0.1, 0.5]],
});
const COOLDOWN = item({
  key: 'production.risk.rules.circuit_breaker.cooldown',
  label: 'Breaker cooldown',
  value: 'rest_of_month',
  default: 'rest_of_month',
  choices: ['rest_of_month', 'none'],
});
const BACKENDS = item({
  key: 'notify.backends',
  group: 'notifications',
  label: 'Where system alerts go',
  value: ['log', 'store'],
  default: ['log', 'store'],
});
const OFFSET = item({
  key: 'scheduler.jobs.tick.trigger.offset_minutes',
  group: 'schedule',
  label: 'tick: minutes after the close',
  applies: 'restart',
  value: 45,
  default: 45,
});
const JOB = item({
  key: 'scheduler.jobs.tick.enabled',
  group: 'schedule',
  label: 'tick: on',
  applies: 'restart',
  value: true,
  default: true,
});

const ITEMS = [
  MAX_WEIGHT,
  MAX_POSITIONS,
  ENABLED,
  COOLDOWN,
  UNIVERSE,
  MODEL_BOOKS,
  SCHEDULE,
  BACKENDS,
  JOB,
  OFFSET,
];

describe('system settings values', () => {
  it('reads how to edit a setting from its value, default and choices', () => {
    expect(kindOf(MAX_WEIGHT)).toBe('percent');
    expect(kindOf(BACKENDS)).toBe('multi');
    expect(kindOf(MAX_POSITIONS)).toBe('number');
    expect(kindOf(item({ value: null, default: null, help: 'A name.' }))).toBe('text');
    expect(kindOf(ENABLED)).toBe('bool');
    expect(kindOf(UNIVERSE)).toBe('choice');
    expect(kindOf(SCHEDULE)).toBe('json');
    expect(kindOf(item({ value: ['log'], default: ['log', 'store'] }))).toBe('list');
    expect(kindOf(item({ value: 3, default: null }))).toBe('number');
  });

  it('lets an optional limit be empty and checks numbers', () => {
    const cap = item({ value: 10, default: null, help: 'Empty means no limit.' });
    expect(parseRaw(cap, '')).toEqual({ value: null });
    expect(parseRaw(cap, 'x')).toEqual({ error: 'Enter a number.' });
    expect(parseRaw(MAX_POSITIONS, '7')).toEqual({ value: 7 });
  });

  it('takes fractions as percents and checks the range before any code is asked', () => {
    expect(toRaw(MAX_WEIGHT, 0.25)).toBe('25');
    expect(toRaw(MAX_WEIGHT, 0.07)).toBe('7');
    expect(parseRaw(MAX_WEIGHT, '25')).toEqual({ value: 0.25 });
    expect(parseRaw(MAX_WEIGHT, '7')).toEqual({ value: 0.07 });
    expect(parseRaw(MAX_WEIGHT, '')).toEqual({ error: 'Enter a percent from 0 to 100.' });
    expect(parseRaw(MAX_WEIGHT, '250')).toEqual({ error: 'Enter a percent from 0 to 100.' });
    expect(valueText(0.25, MAX_WEIGHT)).toBe('25%');
  });

  it('names jobs and values in words, never by their ids', () => {
    expect(settingLabel(JOB)).toBe('Trading run: on');
    expect(settingLabel(OFFSET)).toBe('Trading run: minutes after the close');
    expect(settingLabel(MAX_WEIGHT)).toBe('Largest holding');
    expect(valueText('rest_of_month', COOLDOWN)).toBe('Until the month ends');
    expect(valueText('shadow', MODEL_BOOKS)).toBe('Only strategies On trial');
    expect(valueText(['log', 'outbox'], BACKENDS)).toBe('The server log, Admin devices');
    expect(valueText(null, MAX_POSITIONS)).toBe('no limit');
  });

  it('keeps a universe that is not one of the choices', () => {
    expect(toRaw(UNIVERSE, UNIVERSE.value)).toBe(KEEP_CURRENT);
    expect(parseRaw(UNIVERSE, KEEP_CURRENT)).toEqual({ value: ['AAA.US', 'BBB.US'] });
    expect(parseRaw(UNIVERSE, 'sp500')).toEqual({ value: 'sp500' });
    expect(parseRaw(UNIVERSE, 'nope')).toEqual({ error: 'Pick one of the options.' });
  });

  it('reads lists and pairs', () => {
    const list = item({ value: ['log'], default: ['log'] });
    expect(parseRaw(list, 'log,  store\nwebhook')).toEqual({ value: ['log', 'store', 'webhook'] });
    expect(toRaw(SCHEDULE, SCHEDULE.value)).toBe('[[0.1,0.5]]');
    expect(parseRaw(SCHEDULE, '[[0.2, 0]]')).toEqual({ value: [[0.2, 0]] });
    expect(parseRaw(SCHEDULE, '[[0.2')).toHaveProperty('error');
  });

  it('writes values in words', () => {
    expect(valueText(null)).toBe('none');
    expect(valueText(false)).toBe('Off');
    expect(valueText(['AAA.US', 'BBB.US'])).toBe('AAA.US, BBB.US');
    expect(valueText(0.25)).toBe('0.25');
  });

  it("reads the server's key: message from a 422", () => {
    const err = new ApiError(422, 'Invalid request', 'x', [], null, null, {
      detail: 'production.risk.max_weight_per_ticker: Input should be less than or equal to 1',
    });
    expect(keyError(err, MAX_WEIGHT.key)).toBe('Input should be less than or equal to 1');
    expect(keyError(new ApiError(500, 'Error', 'boom'), MAX_WEIGHT.key)).toBeNull();
  });
});

describe('SystemSettings', () => {
  let fixture: ComponentFixture<SystemSettings>;
  let http: HttpTestingController;
  let el: HTMLElement;
  let ensure: MockInstance<StepUpService['ensure']>;

  async function render(items: SystemSettingView[] = ITEMS): Promise<void> {
    TestBed.configureTestingModule({ providers: [...provideApi(), provideHttpClientTesting()] });
    http = TestBed.inject(HttpTestingController);
    ensure = vi.spyOn(TestBed.inject(StepUpService), 'ensure').mockResolvedValue(true);
    fixture = TestBed.createComponent(SystemSettings);
    el = fixture.nativeElement;
    fixture.detectChanges();
    (await nextRequest(http, '/api/settings/system')).flush({ items });
    await settle();
  }

  async function settle(): Promise<void> {
    for (let i = 0; i < 4; i++) {
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

  it('groups the settings with plain labels, defaults and who changed them', async () => {
    await render();
    const folds = [...el.querySelectorAll<HTMLDetailsElement>('details.group')];
    expect(folds.map((d) => d.querySelector('.group-title')?.textContent?.trim())).toEqual([
      'Risk limits',
      'Trading run',
      'System alerts',
      'Schedule',
    ]);
    // Risk limits open, the rest folded, so the page starts short.
    expect(folds.map((d) => d.open)).toEqual([true, false, false, false]);
    const text = el.textContent ?? '';
    expect(text).toContain('Largest holding');
    expect(text).toContain('Default: 25%.');
    expect(text).toContain('Default: no limit.');
    expect(text).toContain('Trading run: on');
    expect(text).toContain('Trading run: minutes after the close');
    expect(text).not.toContain('tick:');
    expect(text).not.toContain('rest_of_month');
    expect(text).not.toContain('None');
    expect(text).toContain('Used from the next trading run.');
    expect(text).toContain('Takes effect when the scheduler restarts.');
    expect(text).toContain('by Ada Admin: Keep every record');
    expect(text).not.toContain('production.risk');
    expect(el.querySelector(`label[for="set-${MAX_WEIGHT.key}"]`)).not.toBeNull();
    expect(input(MAX_WEIGHT.key).getAttribute('aria-describedby')).toContain('-hint');
    expect(saveButton().disabled).toBe(true);
  });

  it('shows choices as a select, the universe included', async () => {
    await render();
    const universe = el.querySelector<HTMLSelectElement>(`select[id="set-${UNIVERSE.key}"]`)!;
    const options = [...universe.options].map((o) => o.textContent?.trim());
    expect(options).toEqual(['AAA.US, BBB.US', 'starter', 'sp500']);
    const books = el.querySelector<HTMLSelectElement>(`select[id="set-${MODEL_BOOKS.key}"]`)!;
    expect([...books.options].map((o) => o.textContent?.trim())).toEqual([
      'Every strategy',
      'Only strategies On trial',
    ]);
  });

  it('puts the pairs of numbers under Advanced', async () => {
    await render();
    const advanced = el.querySelector<HTMLDetailsElement>('details.advanced')!;
    expect(advanced.querySelector('summary')?.textContent?.trim()).toBe('Advanced');
    expect(advanced.open).toBe(false);
    expect(advanced.querySelector(`[id="set-${SCHEDULE.key}"]`)).not.toBeNull();
  });

  it('picks where alerts go with checkboxes', async () => {
    await render();
    const outbox = el.querySelector<HTMLInputElement>(`[id="set-${BACKENDS.key}-outbox"]`)!;
    expect(outbox.checked).toBe(false);
    outbox.click();
    fixture.detectChanges();
    type(el.querySelector<HTMLInputElement>('#ops-settings-reason')!, 'Push to admins');
    saveButton().click();
    await settle();
    const put = await nextRequest(http, `/api/settings/system/${BACKENDS.key}`, 'PUT');
    expect(put.request.body).toEqual({
      value: ['log', 'store', 'outbox'],
      reason: 'Push to admins',
    });
    put.flush({ ...BACKENDS, value: ['log', 'store', 'outbox'], overridden: true });
    await settle();
  });

  it('refuses a percent over 100 before asking for the code', async () => {
    await render();
    type(input(MAX_WEIGHT.key), '250');
    type(el.querySelector<HTMLInputElement>('#ops-settings-reason')!, 'Try a big cap');
    saveButton().click();
    await settle();
    expect(ensure).not.toHaveBeenCalled();
    expect(el.textContent).toContain('Enter a percent from 0 to 100.');
  });

  it('keeps Save in a sticky bar while there are changes', async () => {
    await render();
    const bar = el.querySelector('.save-bar')!;
    expect(bar.classList.contains('active')).toBe(false);
    type(input(MAX_WEIGHT.key), '20');
    expect(bar.classList.contains('active')).toBe(true);
    expect(el.querySelector('details.group .group-count')?.textContent?.trim()).toBe('1 change');
  });

  it('needs a reason before sending', async () => {
    await render();
    type(input(MAX_WEIGHT.key), '20');
    saveButton().click();
    await settle();
    expect(el.textContent).toContain('Say why in a few words');
    expect(ensure).not.toHaveBeenCalled();
  });

  it('asks for the second factor, then sends one PUT per changed setting', async () => {
    await render();
    const success = vi.spyOn(TestBed.inject(ToastService), 'success');
    type(input(MAX_WEIGHT.key), '20');
    const universe = el.querySelector<HTMLSelectElement>(`select[id="set-${UNIVERSE.key}"]`)!;
    universe.value = 'sp500';
    universe.dispatchEvent(new Event('change'));
    type(el.querySelector<HTMLInputElement>('#ops-settings-reason')!, 'Retail defaults');
    expect(saveButton().textContent).toContain('Save 2 changes');
    saveButton().click();
    await settle();
    expect(ensure).toHaveBeenCalledWith('Change system settings');
    const first = await nextRequest(http, `/api/settings/system/${MAX_WEIGHT.key}`, 'PUT');
    expect(first.request.body).toEqual({ value: 0.2, reason: 'Retail defaults' });
    first.flush({ ...MAX_WEIGHT, value: 0.2, overridden: true });
    await settle();
    const second = await nextRequest(http, `/api/settings/system/${UNIVERSE.key}`, 'PUT');
    expect(second.request.body).toEqual({ value: 'sp500', reason: 'Retail defaults' });
    second.flush({ ...UNIVERSE, value: 'sp500', overridden: true });
    await settle();
    expect(success).toHaveBeenCalledWith('Saved 2 settings.');
    expect(input(MAX_WEIGHT.key).value).toBe('20');
    expect(saveButton().disabled).toBe(true);
  });

  it('saves once when Save is pressed twice during the code check', async () => {
    await render();
    let pass!: (ok: boolean) => void;
    ensure.mockReturnValue(new Promise<boolean>((resolve) => (pass = resolve)));
    type(input(MAX_WEIGHT.key), '20');
    type(el.querySelector<HTMLInputElement>('#ops-settings-reason')!, 'Retail defaults');
    saveButton().click();
    saveButton().click();
    pass(true);
    await settle();
    const puts = http.match(`/api/settings/system/${MAX_WEIGHT.key}`);
    expect(puts.length).toBe(1);
    puts.forEach((r) => r.flush({ ...MAX_WEIGHT, value: 0.2, overridden: true }));
    await settle();
  });

  it('sends nothing when the second factor is cancelled', async () => {
    await render();
    ensure.mockResolvedValue(false);
    type(input(MAX_WEIGHT.key), '20');
    type(el.querySelector<HTMLInputElement>('#ops-settings-reason')!, 'Retail defaults');
    saveButton().click();
    await settle();
    http.expectNone(`/api/settings/system/${MAX_WEIGHT.key}`);
  });

  it('marks the field the server refused', async () => {
    await render();
    type(input(MAX_WEIGHT.key), '100');
    type(el.querySelector<HTMLInputElement>('#ops-settings-reason')!, 'Try a big cap');
    saveButton().click();
    await settle();
    const put = await nextRequest(http, `/api/settings/system/${MAX_WEIGHT.key}`, 'PUT');
    put.flush(
      {
        title: 'Invalid request',
        status: 422,
        detail: `${MAX_WEIGHT.key}: Input should be less than or equal to 1`,
      },
      { status: 422, statusText: 'Unprocessable Entity' },
    );
    await settle();
    expect(el.textContent).toContain('Input should be less than or equal to 1');
    expect(input(MAX_WEIGHT.key).getAttribute('aria-invalid')).toBe('true');
    expect(el.querySelector('[role="alert"]')).not.toBeNull();
  });

  it('puts a changed setting back to its default', async () => {
    await render();
    type(el.querySelector<HTMLInputElement>('#ops-settings-reason')!, 'Back to the file');
    const reset = [...el.querySelectorAll<HTMLButtonElement>('button')].find(
      (b) => b.textContent?.trim() === 'Use the default',
    )!;
    reset.click();
    await settle();
    const post = await nextRequest(http, `/api/settings/system/${MODEL_BOOKS.key}/reset`, 'POST');
    expect(post.request.body).toEqual({ reason: 'Back to the file' });
    post.flush({ ...MODEL_BOOKS, overridden: false, updated_at: null });
    await settle();
    expect(el.textContent).not.toContain('Use the default');
  });
});
