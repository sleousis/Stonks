import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';

import { provideApi } from '../../api/provide-api';
import { TRADER } from '../../../testing/auth-fixtures';
import { nextRequest } from '../../../testing/http';
import { ToastService } from '../notify/toast.service';
import { SessionService } from './session.service';
import { StepUpService } from './step-up.service';

describe('StepUpService', () => {
  let stepUp: StepUpService;
  let session: SessionService;
  let controller: HttpTestingController;

  beforeEach(() => {
    sessionStorage.clear();
    TestBed.configureTestingModule({
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting()],
    });
    stepUp = TestBed.inject(StepUpService);
    session = TestBed.inject(SessionService);
    controller = TestBed.inject(HttpTestingController);
  });

  afterEach(() => controller.verify());

  async function signIn(me = TRADER) {
    const loading = session.load();
    (await nextRequest(controller, '/api/auth/me')).flush(me);
    await loading;
  }

  it('opens a request and resolves true once a code is accepted', async () => {
    await signIn();
    const result = stepUp.prompt('Turn on auto');
    expect(stepUp.request()?.reason).toBe('Turn on auto');

    const submitted = stepUp.submit({ code: '123456' });
    const req = await nextRequest(controller, '/api/auth/mfa/verify', 'POST');
    expect(req.request.body).toEqual({ code: '123456' });
    req.flush({ method: 'totp', csrf_token: null, recovery_codes_left: 10 });
    await submitted;

    expect(await result).toBe(true);
    expect(stepUp.request()).toBeNull();
    expect(session.me()?.mfa_fresh).toBe(true);
  });

  it('keeps the request open when the code is wrong', async () => {
    await signIn();
    const result = stepUp.prompt();
    const submitted = stepUp.submit({ code: '000000' });
    (await nextRequest(controller, '/api/auth/mfa/verify', 'POST')).flush(
      { title: 'Unauthorized', status: 401, detail: 'invalid_credentials' },
      { status: 401, statusText: 'Unauthorized' },
    );
    await expect(submitted).rejects.toMatchObject({ code: 'invalid_credentials' });
    expect(stepUp.request()).not.toBeNull();
    stepUp.cancel();
    expect(await result).toBe(false);
  });

  it('shares one prompt between concurrent asks', async () => {
    await signIn();
    const a = stepUp.prompt();
    const b = stepUp.prompt();
    stepUp.cancel();
    expect(await a).toBe(false);
    expect(await b).toBe(false);
  });

  it('refuses with a toast when signed in with an API token', async () => {
    await signIn({ ...TRADER, via: 'token' });
    const error = vi.spyOn(TestBed.inject(ToastService), 'error');
    expect(await stepUp.prompt()).toBe(false);
    expect(error).toHaveBeenCalled();
    expect(stepUp.request()).toBeNull();
  });

  it('ensure() skips the prompt while the last check is fresh', async () => {
    await signIn();
    const ok = stepUp.ensure('Turn on auto');
    (await nextRequest(controller, '/api/auth/me')).flush({ ...TRADER, mfa_fresh: true });
    expect(await ok).toBe(true);
    expect(stepUp.request()).toBeNull();
  });

  it('ensure() prompts when the check is stale', async () => {
    await signIn();
    const ok = stepUp.ensure('Turn on auto');
    (await nextRequest(controller, '/api/auth/me')).flush(TRADER);
    await vi.waitFor(() => expect(stepUp.request()).not.toBeNull());
    stepUp.cancel();
    expect(await ok).toBe(false);
  });

  it('ensure() with /me status 0 keeps me and shows no token toast (UX-36)', async () => {
    await signIn();
    const error = vi.spyOn(TestBed.inject(ToastService), 'error');
    const ok = stepUp.ensure('Resume trading');
    (await nextRequest(controller, '/api/auth/me')).error(new ProgressEvent('error'), {
      status: 0,
    });
    await vi.waitFor(() => expect(stepUp.request()).not.toBeNull());
    expect(session.me()?.user_id).toBe('usr_1');
    expect(error).not.toHaveBeenCalled();
    stepUp.cancel();
    expect(await ok).toBe(false);
  });

  it('ensure() sends a signed-out user to sign in (UX-36)', async () => {
    await signIn();
    const nav = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    const ok = stepUp.ensure('Resume trading');
    (await nextRequest(controller, '/api/auth/me')).flush(
      { title: 'x', status: 401, detail: 'not_authenticated' },
      { status: 401, statusText: 'Unauthorized' },
    );
    expect(await ok).toBe(false);
    expect(stepUp.request()).toBeNull();
    expect(nav).toHaveBeenCalledWith(['/login'], { queryParams: { next: '/' } });
  });
});
