import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';

import type { OptionsLiveView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { StepUpService } from '../../core/auth/step-up.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { nextRequest, tick } from '../../../testing/http';
import { OPTIONS_OFF } from '../../../testing/fake-options-live';
import { LiveOptionsCard, levelLabel, optionsHeadline } from './live-options-card';

describe('live options words', () => {
  it('says whether options are live', () => {
    expect(optionsHeadline({ allowed: false })).toBe('Options live: off');
    expect(optionsHeadline({ allowed: true })).toBe('Options live: on');
    expect(levelLabel('spreads')).toBe('Spreads');
  });
});

describe('LiveOptionsCard', () => {
  let fixture: ComponentFixture<LiveOptionsCard>;
  let http: HttpTestingController;
  let allowed: boolean;
  let confirm: ReturnType<typeof vi.spyOn>;
  let ensure: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    allowed = true;
    TestBed.configureTestingModule({ providers: [...provideApi(), provideHttpClientTesting()] });
    http = TestBed.inject(HttpTestingController);
    const session = TestBed.inject(SessionService);
    vi.spyOn(session, 'can').mockImplementation(() => allowed);
    vi.spyOn(session, 'whyNot').mockImplementation(() => (allowed ? null : 'Traders only.'));
    confirm = vi.spyOn(TestBed.inject(ConfirmService), 'confirm').mockResolvedValue(true);
    ensure = vi.spyOn(TestBed.inject(StepUpService), 'ensure').mockResolvedValue(true);
  });

  afterEach(() => http.verify());

  async function render(view: OptionsLiveView = OPTIONS_OFF): Promise<HTMLElement> {
    fixture = TestBed.createComponent(LiveOptionsCard);
    fixture.componentRef.setInput('portfolioId', 'pf_live');
    fixture.componentRef.setInput('portfolioName', 'Main live');
    fixture.detectChanges();
    (await nextRequest(http, '/api/portfolios/pf_live/live/options')).flush(view);
    await tick();
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  function radio(el: HTMLElement, text: string) {
    return [...el.querySelectorAll<HTMLButtonElement>('[role="radio"]')].find(
      (b) => b.textContent?.trim() === text,
    )!;
  }

  function type(el: HTMLElement, value: string) {
    const input = el.querySelector<HTMLTextAreaElement>('#options-level-reason')!;
    input.value = value;
    input.dispatchEvent(new Event('input'));
  }

  it('shows options live off with every reason', async () => {
    const el = await render();
    expect(el.querySelector('[data-testid="options-headline"]')?.textContent).toContain(
      'Options live: off',
    );
    expect(el.querySelectorAll('.reasons li').length).toBe(3);
    // Off on the server: one line, the rest folded (F58), stage ids in trader words.
    expect(el.querySelector('[data-testid="options-off-line"]')?.textContent).toContain(
      'Not available on this server',
    );
    expect(el.querySelector('details.more')?.hasAttribute('open')).toBe(false);
    expect(el.querySelector('.reasons')?.textContent).toContain(
      'at stage Broker paper, not Real money, small or higher',
    );
    expect(el.querySelector('[data-testid="options-level"]')?.textContent).toContain('None');
    expect(el.textContent).toContain('Wait for approval');
  });

  it('sets the level after the ticket and a fresh code', async () => {
    const el = await render();
    radio(el, 'Covered').click();
    type(el, 'IBKR granted level 2');
    fixture.detectChanges();
    el.querySelector<HTMLFormElement>('form')!.dispatchEvent(new Event('submit'));
    const put = await nextRequest(http, '/api/portfolios/pf_live/live/options/approval', 'PUT');
    expect(put.request.body).toEqual({ level: 'covered', reason: 'IBKR granted level 2' });
    const options = confirm.mock.calls[0][0] as { tone: string; ticket: { live: boolean } };
    expect(options.tone).toBe('danger');
    expect(options.ticket.live).toBe(true);
    expect(ensure).toHaveBeenCalledTimes(1);
    put.flush({ ...OPTIONS_OFF, level: 'covered', reason: 'IBKR granted level 2' });
    await tick();
    fixture.detectChanges();
    expect(el.querySelector('[data-testid="options-level"]')?.textContent).toContain('Covered');
  });

  it('names its reason apart from the allocation reason', async () => {
    const el = await render();
    expect(el.querySelector('label[for="options-level-reason"]')?.textContent?.trim()).toBe(
      'Reason for the options level',
    );
  });

  it('needs a reason and a change before it asks', async () => {
    const el = await render();
    radio(el, 'Spreads').click();
    fixture.detectChanges();
    el.querySelector<HTMLFormElement>('form')!.dispatchEvent(new Event('submit'));
    await tick();
    fixture.detectChanges();
    expect(el.textContent).toContain('Say why.');
    expect(confirm).not.toHaveBeenCalled();
  });

  it('shows options live on when every condition holds', async () => {
    const el = await render({ ...OPTIONS_OFF, enabled: true, allowed: true, reasons: [] });
    expect(el.querySelector('[data-testid="options-headline"]')?.textContent).toContain(
      'Options live: on',
    );
    expect(el.querySelector('.reasons')).toBeNull();
    expect(el.querySelector('[data-testid="options-off-line"]')).toBeNull();
    expect(el.querySelector('details.more')?.hasAttribute('open')).toBe(true);
  });

  it('cannot change the level without live.manage', async () => {
    allowed = false;
    const el = await render();
    const submit = el.querySelector<HTMLButtonElement>('button[type="submit"]')!;
    expect(submit.disabled).toBe(true);
  });
});
