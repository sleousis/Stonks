import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { Draft } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { nextRequest, tick } from '../../../testing/http';
import { makeDraft } from '../../../testing/studio-fixtures';
import { DraftShip } from './draft-ship';

describe('DraftShip', () => {
  let fixture: ComponentFixture<DraftShip>;
  let controller: HttpTestingController;
  let el: HTMLElement;
  let confirm: ReturnType<typeof vi.fn>;
  let emitted: Draft[];

  function setup(draft: Draft, answer = true): void {
    confirm = vi.fn().mockResolvedValue(answer);
    TestBed.configureTestingModule({
      imports: [DraftShip],
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        provideRouter([]),
        { provide: ConfirmService, useValue: { confirm } },
      ],
    });
    controller = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(DraftShip);
    fixture.componentRef.setInput('draft', draft);
    emitted = [];
    fixture.componentInstance.changed.subscribe((d) => emitted.push(d));
    fixture.detectChanges();
    el = fixture.nativeElement;
  }

  afterEach(() => controller.verify());

  const registered = (status: 'shadow' | 'active') =>
    makeDraft({
      status: 'registered',
      registered_strategy_id: 'studio_rsi_dip_buyer',
      strategy_status: status,
    });

  function buttonNamed(text: string): HTMLButtonElement {
    const b = [...el.querySelectorAll('button')].find((x) => x.textContent?.includes(text));
    if (!b) throw new Error(`no button ${text}`);
    return b;
  }

  it('registers in shadow only after the trader confirms', async () => {
    setup(makeDraft());
    expect(el.textContent).toContain('Not registered');

    buttonNamed('Register in shadow').click();
    await tick();
    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({ title: 'Register RSI dip buyer?', confirmLabel: 'Register' }),
    );

    const req = await nextRequest(controller, '/api/studio/drafts/draft_abc123/register', 'POST');
    req.flush(registered('shadow'));
    await tick();
    expect(emitted.at(-1)?.strategy_status).toBe('shadow');
  });

  it('does nothing when the register confirmation is cancelled', async () => {
    setup(makeDraft(), false);
    buttonNamed('Register in shadow').click();
    await tick(10);
    expect(confirm).toHaveBeenCalled();
    expect(emitted).toEqual([]);
  });

  it('saves pending edits before registering', async () => {
    setup(makeDraft());
    const ensureSaved = vi.fn().mockResolvedValue(false);
    fixture.componentRef.setInput('ensureSaved', ensureSaved);
    buttonNamed('Register in shadow').click();
    await tick(10);
    expect(ensureSaved).toHaveBeenCalled();
    // Saving failed, so no register request went out (verify() checks it).
  });

  it('enables a shadow strategy with a typed confirmation', async () => {
    setup(registered('shadow'));
    const toggle = el.querySelector('[role="switch"]') as HTMLButtonElement;
    expect(toggle.getAttribute('aria-checked')).toBe('false');

    toggle.click();
    await tick();
    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({
        confirmLabel: 'Enable',
        typedConfirmation: 'studio_rsi_dip_buyer',
      }),
    );
    const req = await nextRequest(controller, '/api/studio/drafts/draft_abc123/enable', 'POST');
    req.flush(registered('active'));
    await tick();
    expect(emitted.at(-1)?.strategy_status).toBe('active');

    fixture.componentRef.setInput('draft', emitted.at(-1));
    fixture.detectChanges();
    expect(toggle.getAttribute('aria-checked')).toBe('true');
    expect(el.textContent).toContain('Enabled');
  });

  it('disables an active strategy back to shadow after a danger confirmation', async () => {
    setup(registered('active'));
    (el.querySelector('[role="switch"]') as HTMLButtonElement).click();
    await tick();
    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({ confirmLabel: 'Disable', tone: 'danger' }),
    );
    const req = await nextRequest(controller, '/api/studio/drafts/draft_abc123/disable', 'POST');
    req.flush(registered('shadow'));
    await tick();
    expect(emitted.at(-1)?.strategy_status).toBe('shadow');
  });

  it('does not offer a toggle for a retired strategy', () => {
    setup(
      makeDraft({
        status: 'registered',
        registered_strategy_id: 'old',
        strategy_status: 'retired',
      }),
    );
    expect(el.querySelector('[role="switch"]')).toBeNull();
    expect(el.textContent).toContain('retired');
  });
});
