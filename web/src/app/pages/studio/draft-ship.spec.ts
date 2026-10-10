import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { Draft } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { LIFECYCLE } from '../../shared/governance-labels';
import { nextRequest, page, tick } from '../../../testing/http';
import {
  answerDialog,
  dialogForm,
  goLiveReport,
  isHoldDialog,
} from '../../../testing/status-dialog';
import { makeDraft } from '../../../testing/studio-fixtures';
import { DraftShip } from './draft-ship';

describe('DraftShip', () => {
  let fixture: ComponentFixture<DraftShip>;
  let controller: HttpTestingController;
  let el: HTMLElement;
  let confirm: ReturnType<typeof vi.fn>;
  let emitted: Draft[];

  /** `allowed`: true for an admin, false for a viewer, 'trader' for a trader. */
  function setup(draft: Draft, answer = true, allowed: boolean | 'trader' = true): void {
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
    const session = TestBed.inject(SessionService);
    const can = (p: string) => (allowed === 'trader' ? p !== 'strategy.promote' : allowed);
    vi.spyOn(session, 'can').mockImplementation((p) => can(p));
    vi.spyOn(session, 'whyNot').mockImplementation((p) =>
      can(p) ? null : 'Traders and admins only.',
    );
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

  it('puts it on trial only after the trader confirms', async () => {
    setup(makeDraft());
    expect(el.textContent).toContain('Draft');

    buttonNamed(LIFECYCLE.paper.label).click();
    await tick();
    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({
        title: 'Put RSI dip buyer on trial?',
        confirmLabel: LIFECYCLE.paper.label,
      }),
    );

    const req = await nextRequest(controller, '/api/studio/drafts/draft_abc123/register', 'POST');
    req.flush(registered('shadow'));
    await tick();
    expect(emitted.at(-1)?.strategy_status).toBe('shadow');
  });

  it('does nothing when the register confirmation is cancelled', async () => {
    setup(makeDraft(), false);
    buttonNamed(LIFECYCLE.paper.label).click();
    await tick(10);
    expect(confirm).toHaveBeenCalled();
    expect(emitted).toEqual([]);
  });

  it('saves pending edits before registering', async () => {
    setup(makeDraft());
    const ensureSaved = vi.fn().mockResolvedValue(false);
    fixture.componentRef.setInput('ensureSaved', ensureSaved);
    buttonNamed(LIFECYCLE.paper.label).click();
    await tick(10);
    expect(ensureSaved).toHaveBeenCalled();
    // Saving failed, so no register request went out (verify() checks it).
  });

  it('approves with a reason and a hold after the go-live check', async () => {
    setup(registered('shadow'));
    expect(el.querySelector('[role="switch"]')).toBeNull();

    buttonNamed(LIFECYCLE.live.label).click();
    (await nextRequest(controller, '/api/strategies/studio_rsi_dip_buyer/golive')).flush(
      goLiveReport('studio_rsi_dip_buyer', true),
    );
    // The approval ticket reads the broker and who follows it, like the strategy page.
    (await nextRequest(controller, '/api/brokers')).flush({
      kind: 'simulated',
      paper: true,
      allow_live: false,
      credentials_configured: false,
    });
    (await nextRequest(controller, '/api/subscriptions')).flush(page([]));
    await tick(5);
    fixture.detectChanges();
    expect(dialogForm(el)?.textContent).toContain('Go-live check passed');
    expect(el.querySelector('app-mode-stamp')?.textContent).toContain('PAPER');
    expect(isHoldDialog(el)).toBe(true);
    answerDialog(fixture, { reason: 'Shadow run looked right' });

    const req = await nextRequest(controller, '/api/studio/drafts/draft_abc123/enable', 'POST');
    expect(req.request.body).toEqual({ reason: 'Shadow run looked right', override: false });
    req.flush(registered('active'));
    await tick();
    expect(emitted.at(-1)?.strategy_status).toBe('active');

    fixture.componentRef.setInput('draft', emitted.at(-1));
    fixture.detectChanges();
    expect(buttonNamed(LIFECYCLE.pause.label)).toBeDefined();
    expect(el.textContent).toContain('approved');
    expect(el.textContent).not.toMatch(/(?<!go-)\blive\b/i);
  });

  it('puts an approved strategy back on trial with a reason, never red', async () => {
    setup(registered('active'));
    buttonNamed(LIFECYCLE.pause.label).click();
    await tick();
    fixture.detectChanges();
    expect(dialogForm(el)?.querySelector('button.btn-danger')).toBeNull();
    expect(dialogForm(el)?.querySelector('button[type="submit"]')?.textContent).toContain(
      LIFECYCLE.pause.label,
    );
    answerDialog(fixture, { reason: 'Spread widened' });
    const req = await nextRequest(controller, '/api/studio/drafts/draft_abc123/disable', 'POST');
    expect(req.request.body).toEqual({ reason: 'Spread widened', override: false });
    req.flush(registered('shadow'));
    await tick();
    expect(emitted.at(-1)?.strategy_status).toBe('shadow');
  });

  it('does not offer to approve a retired strategy', () => {
    setup(
      makeDraft({
        status: 'registered',
        registered_strategy_id: 'old',
        strategy_status: 'retired',
      }),
    );
    expect([...el.querySelectorAll('button')].map((b) => b.textContent?.trim())).not.toContain(
      LIFECYCLE.live.label,
    );
    expect(el.textContent).toContain('retired');
  });

  describe('permissions (UI-06)', () => {
    it('disables Put on trial with a reason without strategy.trial', async () => {
      setup(makeDraft(), true, false);
      const button = buttonNamed(LIFECYCLE.paper.label);
      expect(button.disabled).toBe(true);
      expect(el.querySelector('app-permission-note')?.textContent).toContain('Traders and admins');
      button.click();
      await tick(5);
      expect(confirm).not.toHaveBeenCalled();
    });

    it('lets a trader put their own draft on trial (NB-02)', async () => {
      setup(makeDraft(), true, 'trader');
      const button = buttonNamed(LIFECYCLE.paper.label);
      expect(button.disabled).toBe(false);
      button.click();
      await tick();
      const req = await nextRequest(controller, '/api/studio/drafts/draft_abc123/register', 'POST');
      req.flush(registered('shadow'));
      await tick();
      expect(emitted).toHaveLength(1);
    });

    it('shows a trader the next step instead of Approve', () => {
      setup(registered('shadow'), true, 'trader');
      const labels = [...el.querySelectorAll('button')].map((b) => b.textContent?.trim());
      expect(labels).not.toContain(LIFECYCLE.live.label);
      expect(el.textContent).toContain('An admin approves it after the go-live check.');
      expect(el.textContent).toContain('Follow it');
    });

    it('shows no note for admins', () => {
      setup(registered('active'));
      expect(buttonNamed(LIFECYCLE.pause.label).disabled).toBe(false);
      expect(el.querySelector('app-permission-note')?.textContent ?? '').toBe('');
    });
  });
});
