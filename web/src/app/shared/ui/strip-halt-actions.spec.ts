import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';

import type { HaltView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { StepUpService } from '../../core/auth/step-up.service';
import { HaltStateService } from '../../core/halts/halt-state.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { TRADER } from '../../../testing/auth-fixtures';
import { nextRequest, tick } from '../../../testing/http';
import { StripHaltActions } from './strip-halt-actions';

const KILL = {
  id: 5,
  kind: 'kill',
  scope: 'portfolio',
  portfolio_id: 'pf_paper',
  user_id: 'usr_1',
  halt: 'all',
  reason: 'Pause',
  tripped_by: 'usr_1',
  tripped_at: '2026-10-09T14:00:00Z',
  active: true,
} as HaltView;

describe('StripHaltActions', () => {
  let http: HttpTestingController;
  let ensure: ReturnType<typeof vi.fn>;
  let refresh: ReturnType<typeof vi.fn>;

  beforeEach(async () => {
    ensure = vi.fn().mockResolvedValue(true);
    refresh = vi.fn().mockResolvedValue(undefined);
    const halts = signal<HaltView[]>([KILL]);
    TestBed.configureTestingModule({
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        { provide: StepUpService, useValue: { ensure } },
        {
          provide: HaltStateService,
          useValue: {
            kills: halts,
            active: halts,
            scopeText: signal(() => 'Paper portfolio'),
            refresh,
          },
        },
        { provide: PortfolioContextService, useValue: { options: signal([]) } },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    const loading = TestBed.inject(SessionService).load();
    (await nextRequest(http, '/api/auth/me')).flush(TRADER);
    await loading;
  });

  afterEach(() => http.verify());

  async function settle(fixture: { detectChanges(): void }): Promise<void> {
    for (let i = 0; i < 4; i++) {
      await tick();
      fixture.detectChanges();
    }
  }

  it('resumes a paper stop with the code first and one request', async () => {
    const fixture = TestBed.createComponent(StripHaltActions);
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    [...el.querySelectorAll('button')].find((b) => b.textContent?.trim() === 'Resume')!.click();
    await settle(fixture);
    (await nextRequest(http, '/api/halts/5/resume-checks')).flush({
      halt_id: 5,
      passed: true,
      checks: [],
    });
    await settle(fixture);
    el.querySelector<HTMLButtonElement>('app-resume-sheet button[type="submit"]')!.click();
    fixture.detectChanges();

    const post = await nextRequest(http, '/api/halts/5/resume', 'POST');
    expect(ensure).toHaveBeenCalledWith('Resume trading');
    expect(post.request.body).toEqual({ confirmation: 'RESUME TRADING', reason: 'Resumed by me.' });
    post.flush({ ...KILL, active: false });
    await settle(fixture);
    http.expectNone('/api/halts/5/resume');
    expect(refresh).toHaveBeenCalled();
  });

  it('sends nothing when the code is cancelled', async () => {
    ensure.mockResolvedValue(false);
    const fixture = TestBed.createComponent(StripHaltActions);
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    [...el.querySelectorAll('button')].find((b) => b.textContent?.trim() === 'Resume')!.click();
    await settle(fixture);
    (await nextRequest(http, '/api/halts/5/resume-checks')).flush({
      halt_id: 5,
      passed: true,
      checks: [],
    });
    await settle(fixture);
    el.querySelector<HTMLButtonElement>('app-resume-sheet button[type="submit"]')!.click();
    await settle(fixture);
    expect(ensure).toHaveBeenCalled();
    http.expectNone('/api/halts/5/resume');
  });
});
