import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';

import { TRADER } from '../../../testing/auth-fixtures';
import { nextRequest, tick } from '../../../testing/http';
import { CATALOG, MOMENTUM } from '../../../testing/lab-fixtures';
import type { LabRunRequest, MeView, SurvivalPresetInfo, UniverseView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { LabRunFormView } from './lab-run-form';
import { SURVIVAL_TEST_CATALOG } from './lab-test-fixtures';

describe('LabRunFormView', () => {
  let fixture: ComponentFixture<LabRunFormView>;
  let controller: HttpTestingController;
  let el: HTMLElement;
  let emitted: LabRunRequest[];

  beforeEach(() => {
    emitted = [];
    TestBed.configureTestingModule({
      imports: [LabRunFormView],
      providers: [...provideApi(), provideHttpClientTesting()],
    });
    controller = TestBed.inject(HttpTestingController);
  });

  afterEach(() => controller.verify());

  let presets: SurvivalPresetInfo[] = [];

  async function create(me: MeView = TRADER, catalogStatus = 200): Promise<void> {
    const loading = TestBed.inject(SessionService).load();
    (await nextRequest(controller, '/api/auth/me')).flush(me);
    await loading;
    fixture = TestBed.createComponent(LabRunFormView);
    fixture.componentRef.setInput('classes', CATALOG);
    fixture.componentInstance.submitted.subscribe((r) => emitted.push(r));
    el = fixture.nativeElement;
    fixture.detectChanges();
    const req = await nextRequest(controller, '/api/lab/survival-tests');
    if (catalogStatus === 200) req.flush(SURVIVAL_TEST_CATALOG);
    else
      req.flush({ title: 'x', status: catalogStatus }, { status: catalogStatus, statusText: 'x' });
    (await nextRequest(controller, '/api/lab/survival-presets')).flush(presets);
    await tick();
    fixture.detectChanges();
  }

  function type(selector: string, value: string): void {
    const node = el.querySelector<HTMLInputElement>(selector)!;
    node.value = value;
    node.dispatchEvent(new Event('input'));
    fixture.detectChanges();
  }

  function fillBasics(): void {
    el.querySelector<HTMLInputElement>(`input[value="${MOMENTUM.class_path}"]`)!.click();
    fixture.detectChanges();
    type('#lr-tickers', 'SPY.US');
  }

  function submit(): void {
    el.querySelector<HTMLButtonElement>('button[type="submit"]')!.click();
    fixture.detectChanges();
  }

  it('renders option fields from the API catalog with defaults as placeholders', async () => {
    await create();
    // The quick suite runs oos: its fields come from the stubbed schema.
    const psr = el.querySelector<HTMLInputElement>('#lr-opt-oos-min_psr')!;
    expect(psr.placeholder).toBe('0.95');
    expect(el.querySelector('label[for="lr-opt-oos-min_psr"]')?.textContent).toContain('Min PSR');
    expect(el.querySelector('#lr-opt-oos-min_psr-hint')?.textContent).toContain(
      'Above 0 and below 1.',
    );
    const mode = el.querySelector<HTMLSelectElement>('#lr-opt-oos-mode')!;
    expect([...mode.options].map((o) => o.textContent!.trim())).toEqual([
      'Default (PSR)',
      'PSR',
      'Sharpe',
    ]);
  });

  it('sends only the filled options', async () => {
    await create();
    fillBasics();
    type('#lr-opt-oos-min_trades', '30');
    submit();
    expect(emitted).toHaveLength(1);
    expect(emitted[0].test_options).toEqual({ oos: { min_trades: 30 } });
    expect(emitted[0].preset).toBe('quick');
  });

  it('shows a field error next to the field and opens the panel', async () => {
    await create();
    fillBasics();
    type('#lr-opt-oos-min_psr', '2');
    submit();
    expect(emitted).toEqual([]);
    expect(el.querySelector<HTMLDetailsElement>('details.advanced')!.open).toBe(true);
    expect(el.querySelector('#lr-opt-oos-min_psr-hint')?.textContent).toContain(
      'Must be above 0 and below 1.',
    );
  });

  it('shows a catalog load failure inside the panel with Retry', async () => {
    await create(TRADER, 500);
    expect(el.querySelector('details.advanced')?.textContent).toContain(
      'Could not load the test options',
    );
    [...el.querySelectorAll<HTMLButtonElement>('details.advanced button')]
      .find((b) => b.textContent!.includes('Try again'))!
      .click();
    (await nextRequest(controller, '/api/lab/survival-tests')).flush(SURVIVAL_TEST_CATALOG);
    await tick();
    fixture.detectChanges();
    expect(el.querySelector('#lr-opt-oos-min_psr')).not.toBeNull();
  });

  it("lists the server's tests for a named suite", async () => {
    presets = [{ name: 'quick', tests: ['oos', 'drift'], options: {} }];
    await create();
    presets = [];
    expect(el.querySelector('.suite-tests')?.textContent ?? el.textContent).toContain('Drift');
  });

  it('runs on a stored universe and can fetch missing data first', async () => {
    const universe: UniverseView = { id: 'sp500', kind: 'index', name: 'S&P 500', spec: {} };
    await create();
    fixture.componentRef.setInput('universes', [universe]);
    fixture.detectChanges();
    expect(el.querySelector('#lr-ensure-hint')).toBeNull();
    const pick = el.querySelector<HTMLSelectElement>('#lr-universe')!;
    pick.value = 'sp500';
    pick.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    // The typed tickers step aside for the universe.
    expect(el.querySelector('#lr-tickers')).toBeNull();
    el.querySelector<HTMLInputElement>('input[aria-describedby="lr-ensure-hint"]')!.click();
    fixture.detectChanges();
    el.querySelector<HTMLInputElement>(`input[value="${MOMENTUM.class_path}"]`)!.click();
    fixture.detectChanges();
    submit();
    expect(emitted).toHaveLength(1);
    expect(emitted[0]).toMatchObject({ universe_id: 'sp500', ensure_data: true });
    expect(emitted[0]).not.toHaveProperty('universe');
  });

  it('uses plain labels for the search settings', async () => {
    await create();
    const text = el.textContent!;
    for (const label of ['Share used for tuning', 'Gap before testing', 'Random seed', 'Trials'])
      expect(text).toContain(label);
    for (const jargon of ['Embargo bars', 'Train share', 'Budget'])
      expect(text).not.toContain(jargon);
  });

  describe('the Advanced fold (UX-30)', () => {
    const advanced = () => el.querySelector<HTMLDetailsElement>('details.advanced')!;

    it('seed, embargo and objective sit in a closed details, an error opens it', async () => {
      await create();
      expect(advanced().open).toBe(false);
      for (const id of ['#lr-seed', '#lr-embargo', '#lr-objective', '#lr-budget', '#lr-tuner'])
        expect(advanced().querySelector(id)).not.toBeNull();
      // The trader's own choices stay in view.
      for (const id of ['#lr-tickers', '#lr-hypothesis', '#lr-register', 'input[name="lr-suite"]'])
        expect(advanced().querySelector(id)).toBeNull();

      fillBasics();
      type('#lr-embargo', '-3');
      submit();
      expect(emitted).toEqual([]);
      expect(advanced().open).toBe(true);
      expect(advanced().querySelector('summary')?.textContent).toContain('1 to fix');
    });

    it('stays closed when only a visible field is wrong', async () => {
      await create();
      submit();
      expect(emitted).toEqual([]);
      expect(advanced().open).toBe(false);
    });
  });

  describe('paper trading choice', () => {
    it('says it in trader words and picks the go-live suite', async () => {
      await create();
      const label = el.querySelector('#lr-register')!.closest('label')!;
      expect(label.textContent).toContain('Start paper trading if it passes');
      el.querySelector<HTMLInputElement>('#lr-register')!.click();
      fixture.detectChanges();
      fillBasics();
      type('#lr-hypothesis', 'Slow money chases winners.');
      submit();
      expect(emitted[0]).toMatchObject({ preset: 'promotion', register_if_passes: true });
    });

    it('starts paper trading whatever the verdict only from Advanced', async () => {
      await create();
      el.querySelector<HTMLInputElement>('#lr-register-always')!.click();
      fixture.detectChanges();
      expect(el.querySelector<HTMLInputElement>('#lr-register')!.checked).toBe(true);
      fillBasics();
      type('#lr-hypothesis', 'x');
      submit();
      expect(emitted[0].register_strategy).toBe(true);
      expect(emitted[0].register_if_passes).toBeUndefined();
    });
  });

  it('starts from a prefill: a re-run or ?preset=promotion (UX-22, UX-28)', async () => {
    await create();
    fixture.componentRef.setInput('prefill', {
      classPath: MOMENTUM.class_path,
      tickers: 'NVDA.US',
      suite: 'promotion',
    });
    fixture.detectChanges();
    expect(
      el.querySelector<HTMLInputElement>('input[name="lr-suite"][value="promotion"]')!.checked,
    ).toBe(true);
    submit();
    expect(emitted[0]).toMatchObject({ universe: ['NVDA.US'], preset: 'promotion' });
  });

  it('shows a note instead of Start to someone without lab access', async () => {
    await create({ ...TRADER, role: 'viewer', scopes: ['read'] });
    fillBasics();
    const start = el.querySelector<HTMLButtonElement>('button[type="submit"]')!;
    expect(start.disabled).toBe(true);
    expect(el.querySelector('.permission-note')?.textContent).toContain('Traders and admins only.');
    submit();
    expect(emitted).toEqual([]);
  });
});
