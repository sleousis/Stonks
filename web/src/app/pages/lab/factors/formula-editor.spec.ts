import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';

import { tick } from '../../../../testing/http';
import type { ExpressionCheckView } from '../../../api/models';
import { provideApi } from '../../../api/provide-api';
import { CHECK_DELAY_MS, FormulaEditor } from './formula-editor';

describe('FormulaEditor', () => {
  let fixture: ComponentFixture<FormulaEditor>;
  let controller: HttpTestingController;
  let el: HTMLElement;
  let emitted: (string | null)[];

  beforeEach(async () => {
    TestBed.configureTestingModule({
      imports: [FormulaEditor],
      providers: [...provideApi(), provideHttpClientTesting()],
    });
    controller = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(FormulaEditor);
    emitted = [];
    fixture.componentInstance.valid.subscribe((v) => emitted.push(v));
    el = fixture.nativeElement;
    fixture.detectChanges();
    await tick(5);
  });

  afterEach(() => controller.verify());

  async function typeFormula(text: string): Promise<void> {
    const box = el.querySelector<HTMLTextAreaElement>('#formula-text')!;
    box.value = text;
    box.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    await tick(CHECK_DELAY_MS + 20);
  }

  async function answer(body: ExpressionCheckView): Promise<unknown> {
    const req = controller.expectOne('/api/factors/check');
    const sent = req.request.body;
    req.flush(body);
    await tick(5);
    fixture.detectChanges();
    return sent;
  }

  it('checks as you type and shows the canonical form and warm-up', async () => {
    expect(el.textContent).toContain('Type a formula to check it.');
    await typeFormula('$close/Ref($close,20)-1');
    const sent = await answer({
      ok: true,
      canonical: '$close / Ref($close, 20) - 1',
      lookback_bars: 20,
    });
    expect(sent).toEqual({ expression: '$close/Ref($close,20)-1' });
    const status = el.querySelector('#formula-status')!;
    expect(status.textContent).toContain('Looks good.');
    expect(status.textContent).toContain('$close / Ref($close, 20) - 1');
    expect(status.textContent).toContain('20 bars');
    expect(emitted.at(-1)).toBe('$close/Ref($close,20)-1');
  });

  it('shows why a formula is refused and emits nothing usable', async () => {
    await typeFormula('Ref($close, -1)');
    await answer({ ok: false, error: 'Ref offset -1 reads the future' });
    expect(el.querySelector('#formula-status')!.textContent).toContain('reads the future');
    expect(el.querySelector('#formula-text')!.getAttribute('aria-invalid')).toBe('true');
    expect(emitted.at(-1)).toBeNull();
  });

  it('checks only the last of quick edits', async () => {
    const box = el.querySelector<HTMLTextAreaElement>('#formula-text')!;
    for (const text of ['$c', '$clo', '$close']) {
      box.value = text;
      box.dispatchEvent(new Event('input'));
    }
    await tick(CHECK_DELAY_MS + 20);
    expect(await answer({ ok: true, canonical: '$close', lookback_bars: 0 })).toEqual({
      expression: '$close',
    });
  });
});
