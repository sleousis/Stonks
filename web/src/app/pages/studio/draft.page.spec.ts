import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { DraftValidation } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { provideFakeChart } from '../../../testing/fake-chart';
import { nextRequest, tick } from '../../../testing/http';
import { RSI_TEMPLATE_SPEC, SCHEMA, makeDraft } from '../../../testing/studio-fixtures';
import { DraftPage } from './draft.page';

const VALID: DraftValidation = { valid: true, issues: [] };

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
    fixture = TestBed.createComponent(DraftPage);
    fixture.componentRef.setInput('id', 'draft_abc123');
    fixture.detectChanges();
    el = fixture.nativeElement;
    (await nextRequest(controller, '/api/studio/drafts/draft_abc123')).flush(makeDraft());
    (await nextRequest(controller, '/api/studio/schema')).flush(SCHEMA);
    await settle();
  });

  afterEach(() => {
    // The test and ship tabs load reference data in the background.
    controller.match('/api/lab/cost-models').forEach((r) => r.flush([]));
    controller
      .match((r) => r.url.startsWith('/api/market/instruments'))
      .forEach((r) => r.flush({ items: [], total: 0, limit: 500, offset: 0 }));
    controller.verify();
  });

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
});
