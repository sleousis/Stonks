import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ApplicationRef, signal } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { DraftValidation } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { provideFakeChart } from '../../../testing/fake-chart';
import { nextRequest, tick } from '../../../testing/http';
import { RSI_TEMPLATE_SPEC, SCHEMA, makeDraft } from '../../../testing/studio-fixtures';
import { DraftPage } from './draft.page';

const VALID: DraftValidation = { valid: true, issues: [] };
const allowed = signal(true);

describe('DraftPage', () => {
  let fixture: ComponentFixture<DraftPage>;
  let controller: HttpTestingController;
  let el: HTMLElement;
  let confirm: ReturnType<typeof vi.fn>;

  beforeEach(async () => {
    confirm = vi.fn().mockResolvedValue(true);
    TestBed.configureTestingModule({
      imports: [DraftPage],
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        provideRouter([]),
        provideFakeChart(),
        { provide: ConfirmService, useValue: { confirm } },
      ],
    });
    controller = TestBed.inject(HttpTestingController);
    const session = TestBed.inject(SessionService);
    vi.spyOn(session, 'can').mockImplementation(() => allowed());
    vi.spyOn(session, 'whyNot').mockImplementation(() =>
      allowed() ? null : 'Traders and admins only.',
    );
    fixture = TestBed.createComponent(DraftPage);
    fixture.componentRef.setInput('id', 'draft_abc123');
    fixture.detectChanges();
    el = fixture.nativeElement;
    (await nextRequest(controller, '/api/studio/drafts/draft_abc123')).flush(makeDraft());
    (await nextRequest(controller, '/api/studio/schema')).flush(SCHEMA);
    await settle();
  });

  afterEach(() => {
    allowed.set(true);
    // The test and ship tabs load reference data in the background.
    controller.match('/api/lab/cost-models').forEach((r) => r.flush([]));
    controller.match('/api/lab/survival-presets').forEach((r) => r.flush([]));
    controller
      .match((r) => r.url.startsWith('/api/market/instruments'))
      .forEach((r) => r.flush({ items: [], total: 0, limit: 500, offset: 0 }));
    controller.verify();
  });

  /** Run change detection and after-render hooks (whenStable would wait for background reads). */
  function render(): void {
    TestBed.inject(ApplicationRef).tick();
  }

  async function settle(): Promise<void> {
    for (let i = 0; i < 4; i++) {
      await tick(5);
      fixture.detectChanges();
    }
  }

  /** Answer the debounced background validation. */
  async function answerValidation(result: DraftValidation): Promise<unknown> {
    const req = await nextRequest(controller, '/api/studio/spec/validate', 'POST', 2000);
    req.flush(result);
    await settle();
    return req.request.body;
  }

  it('validates the spec against the API and shows it as valid', async () => {
    const body = (await answerValidation(VALID)) as { spec: unknown };
    expect(body.spec).toEqual({ ...RSI_TEMPLATE_SPEC, description: '' });
    expect(el.querySelector('h1')?.textContent).toContain('RSI dip buyer');
    expect(el.textContent).toContain('Valid');
  });

  it('maps API validation issues to the exact fields and lists them', async () => {
    await answerValidation({
      valid: false,
      issues: [
        { path: 'entry.conditions[0].right.value', message: 'Input should be a finite number' },
        { path: 'sizing.max_positions', message: 'Input should be less than or equal to 100' },
      ],
    });

    expect(el.textContent).toContain('2 problems');
    const summary = el.querySelector('.issues') as HTMLElement;
    expect(summary.textContent).toContain('entry › condition 1 › right › value');
    expect(summary.textContent).toContain('less than or equal to 100');

    const maxPos = el.querySelector('#rb-max-pos') as HTMLInputElement;
    expect(maxPos.getAttribute('aria-invalid')).toBe('true');
    const cond = el.querySelector('[data-node="entry.conditions[0]"]') as HTMLElement;
    expect(cond.textContent).toContain('finite number');

    // "Go to field" focuses the control the issue points at.
    const link = [...summary.querySelectorAll('button')].find((b) =>
      b.textContent?.includes('max_positions'),
    );
    link?.click();
    expect(document.activeElement).toBe(maxPos);
  });

  it('saves the edited spec with PATCH and clears the unsaved state', async () => {
    await answerValidation(VALID);
    const period = el.querySelector('#rb-ind-period-0') as HTMLInputElement;
    period.value = '10';
    period.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    expect(el.textContent).toContain('Unsaved changes');
    await answerValidation(VALID);

    const save = el.querySelector('.save-bar .btn-primary') as HTMLButtonElement;
    save.click();
    const req = await nextRequest(controller, '/api/studio/drafts/draft_abc123', 'PATCH');
    const sent = req.request.body as { spec: { indicators: { period?: number }[] } };
    expect(sent.spec.indicators[0].period).toBe(10);
    req.flush(makeDraft({ spec: sent.spec }));
    await settle();
    expect(el.querySelector('.save-bar')).toBeNull();
  });

  describe('saving while typing (UX-05)', () => {
    const page = () =>
      fixture.componentInstance as unknown as {
        save(): Promise<boolean>;
        dirty(): boolean;
      };

    async function editName(value: string): Promise<void> {
      const name = el.querySelector('#rb-name') as HTMLInputElement;
      name.value = value;
      name.dispatchEvent(new Event('input'));
      fixture.detectChanges();
    }

    it('edit during save keeps dirty true', async () => {
      await answerValidation(VALID);
      await editName('First');
      const saving = page().save();
      const req = await nextRequest(controller, '/api/studio/drafts/draft_abc123', 'PATCH');
      expect((req.request.body as { spec: { name: string } }).spec.name).toBe('First');
      // The trader keeps typing while the request is in flight.
      await editName('Second');
      req.flush(makeDraft({ spec: (req.request.body as { spec: Record<string, unknown> }).spec }));
      expect(await saving).toBe(true);
      await settle();
      expect(page().dirty()).toBe(true);
      expect(el.textContent).toContain('Unsaved changes');
      controller.match('/api/studio/spec/validate').forEach((r) => r.flush(VALID));
    });

    it('two save() calls send one PATCH', async () => {
      await answerValidation(VALID);
      await editName('Once');
      const a = page().save();
      const b = page().save();
      const req = await nextRequest(controller, '/api/studio/drafts/draft_abc123', 'PATCH');
      await tick(5);
      expect(controller.match((r) => r.method === 'PATCH')).toEqual([]);
      req.flush(makeDraft({ spec: (req.request.body as { spec: Record<string, unknown> }).spec }));
      expect(await a).toBe(true);
      expect(await b).toBe(true);
      await settle();
      expect(page().dirty()).toBe(false);
      controller.match('/api/studio/spec/validate').forEach((r) => r.flush(VALID));
    });
  });

  it('a registered draft shows the note (UX-63)', async () => {
    await answerValidation(VALID);
    expect(el.querySelector('#registered-note')).toBeNull();
    fixture.componentRef.setInput('id', 'draft_reg');
    fixture.detectChanges();
    (await nextRequest(controller, '/api/studio/drafts/draft_reg')).flush(
      makeDraft({
        id: 'draft_reg',
        status: 'registered',
        registered_strategy_id: 'rule_rsi_v1',
        strategy_status: 'shadow',
      }),
    );
    await settle();
    const note = el.querySelector('#panel-build #registered-note');
    expect(note?.textContent).toContain('no longer change');
    expect(note?.querySelector('a')?.getAttribute('href')).toBe('/strategies/rule_rsi_v1');
    await answerValidation(VALID);
  });

  it('asks to sign in again, in plain words, when the rule check is refused (UX-63)', async () => {
    const req = await nextRequest(controller, '/api/studio/spec/validate', 'POST', 2000);
    req.flush({ detail: 'no' }, { status: 401, statusText: 'Unauthorized' });
    await settle();
    const text = el.textContent ?? '';
    expect(text).not.toContain('API token');
    expect(text).toContain('Sign in again to check the rules');
  });

  it('asks before leaving with unsaved changes', async () => {
    await answerValidation(VALID);
    expect(await fixture.componentInstance.canLeave()).toBe(true);
    expect(confirm).not.toHaveBeenCalled();

    const name = el.querySelector('#rb-name') as HTMLInputElement;
    name.value = 'Changed';
    name.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    confirm.mockResolvedValueOnce(false);
    expect(await fixture.componentInstance.canLeave()).toBe(false);
    await answerValidation(VALID);
  });

  describe('rename focus (UI-21)', () => {
    const renameButton = () => el.querySelector<HTMLButtonElement>('#draft-rename-button')!;

    it('focuses the name field after render and Cancel returns focus to Rename', async () => {
      await answerValidation(VALID);
      renameButton().click();
      render();
      expect(document.activeElement?.id).toBe('draft-rename');

      const cancel = [...el.querySelectorAll<HTMLButtonElement>('.rename button')].find(
        (b) => b.textContent?.trim() === 'Cancel',
      )!;
      cancel.click();
      render();
      expect(el.querySelector('form.rename')).toBeNull();
      expect(document.activeElement).toBe(renameButton());
    });

    it('returns focus to Rename after saving the new name', async () => {
      await answerValidation(VALID);
      renameButton().click();
      render();
      const input = el.querySelector<HTMLInputElement>('#draft-rename')!;
      input.value = 'Dip buyer v2';
      input.dispatchEvent(new Event('input'));
      fixture.detectChanges();
      (el.querySelector('form.rename') as HTMLFormElement).dispatchEvent(new Event('submit'));
      const req = await nextRequest(controller, '/api/studio/drafts/draft_abc123', 'PATCH');
      req.flush(makeDraft({ name: 'Dip buyer v2' }));
      await settle();
      render();
      expect(el.querySelector('h1')?.textContent).toContain('Dip buyer v2');
      expect(document.activeElement).toBe(renameButton());
    });
  });

  it('says plainly that code strategies are off, with no server settings', async () => {
    await answerValidation(VALID);
    fixture.componentRef.setInput('id', 'draft_code');
    fixture.detectChanges();
    (await nextRequest(controller, '/api/studio/drafts/draft_code')).flush(
      makeDraft({ id: 'draft_code', kind: 'code', source_code: null, spec: {} }),
    );
    await settle();
    const text = el.textContent ?? '';
    expect(text).toContain('Code strategies are turned off on this server. Ask your admin.');
    expect(text).not.toContain('allow_code_strategies');
    expect(text).not.toContain('stonks serve');
    expect(text).not.toContain('[api]');
  });

  it('disables save, rename and checks without lab.run, and skips background validation', async () => {
    await answerValidation(VALID);
    allowed.set(false);
    fixture.detectChanges();
    expect(el.querySelector<HTMLButtonElement>('#draft-rename-button')!.disabled).toBe(true);
    const check = [...el.querySelectorAll<HTMLButtonElement>('button')].find((b) =>
      b.textContent?.includes('Check on sample data'),
    )!;
    expect(check.disabled).toBe(true);
    expect(el.querySelector('app-page-header')?.textContent).toContain('Traders and admins only.');

    const name = el.querySelector('#rb-name') as HTMLInputElement;
    name.value = 'Changed';
    name.dispatchEvent(new Event('input'));
    await settle();
    await tick(500);
    expect(controller.match('/api/studio/spec/validate')).toEqual([]);
    expect((el.querySelector('.save-bar .btn-primary') as HTMLButtonElement).disabled).toBe(true);
  });
});
