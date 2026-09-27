import { HttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { firstValueFrom } from 'rxjs';

import { provideApi } from '../../api/provide-api';
import { FORBIDDEN, TRADER, UNAUTHORIZED, problem } from '../../../testing/auth-fixtures';
import { nextRequest, tick } from '../../../testing/http';
import { ToastService } from '../notify/toast.service';
import { AuthTokenService } from './auth-token.service';
import { SessionService } from './session.service';
import { StepUpService } from './step-up.service';

describe('sessionInterceptor', () => {
  let http: HttpClient;
  let controller: HttpTestingController;
  let session: SessionService;
  let router: Router;

  beforeEach(() => {
    sessionStorage.clear();
    document.cookie = 'stonks_csrf=; Max-Age=0; path=/';
    TestBed.configureTestingModule({
      // provideApi() registers [authInterceptor, errorInterceptor, sessionInterceptor].
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting()],
    });
    http = TestBed.inject(HttpClient);
    controller = TestBed.inject(HttpTestingController);
    session = TestBed.inject(SessionService);
    router = TestBed.inject(Router);
  });

  afterEach(() => controller.verify());

  async function signIn(): Promise<void> {
    const loading = session.load();
    (await nextRequest(controller, '/api/auth/me')).flush(TRADER);
    await loading;
  }

  describe('CSRF', () => {
    it('adds X-CSRF-Token to unsafe API requests made with the session', () => {
      document.cookie = 'stonks_csrf=abc; path=/';
      http.post('/api/notifications/read', {}).subscribe();
      const req = controller.expectOne('/api/notifications/read');
      expect(req.request.headers.get('X-CSRF-Token')).toBe('abc');
      req.flush({});
    });

    it('leaves reads alone', () => {
      document.cookie = 'stonks_csrf=abc; path=/';
      http.get('/api/portfolio').subscribe();
      const req = controller.expectOne('/api/portfolio');
      expect(req.request.headers.has('X-CSRF-Token')).toBe(false);
      req.flush({});
    });

    it('skips it when an API token is sent instead', () => {
      document.cookie = 'stonks_csrf=abc; path=/';
      TestBed.inject(AuthTokenService).setToken('stk_x');
      http.post('/api/ticks', {}).subscribe();
      const req = controller.expectOne('/api/ticks');
      expect(req.request.headers.has('X-CSRF-Token')).toBe(false);
      req.flush({});
    });

    it('never sends it to another origin', () => {
      document.cookie = 'stonks_csrf=abc; path=/';
      http.post('https://example.com/api/x', {}).subscribe();
      const req = controller.expectOne('https://example.com/api/x');
      expect(req.request.headers.has('X-CSRF-Token')).toBe(false);
      req.flush({});
    });
  });

  describe('401', () => {
    it('sends a pending session to the code screen on mfa_required', async () => {
      const nav = vi.spyOn(router, 'navigate').mockResolvedValue(true);
      const result = firstValueFrom(http.get('/api/portfolio'));
      controller
        .expectOne('/api/portfolio')
        .flush(problem(401, 'mfa_required: finish signing in'), UNAUTHORIZED);
      await expect(result).rejects.toBeTruthy();
      expect(session.status()).toBe('mfa-pending');
      expect(nav).toHaveBeenCalledWith(['/login'], {
        queryParams: { step: 'code', next: '/' },
      });
    });

    it('sends an expired session to sign-in', async () => {
      await signIn();
      const nav = vi.spyOn(router, 'navigate').mockResolvedValue(true);
      const result = firstValueFrom(http.get('/api/portfolio'));
      controller.expectOne('/api/portfolio').flush(problem(401, 'not_authenticated'), UNAUTHORIZED);
      await expect(result).rejects.toBeTruthy();
      expect(session.status()).toBe('signed-out');
      expect(nav).toHaveBeenCalledWith(['/login'], { queryParams: { next: '/' } });
    });

    it('stays put in open-reads mode (dev) and leaves the toast to the error interceptor', async () => {
      const nav = vi.spyOn(router, 'navigate');
      const result = firstValueFrom(http.post('/api/ticks', {}));
      controller.expectOne('/api/ticks').flush(problem(401, 'not_authenticated'), UNAUTHORIZED);
      await expect(result).rejects.toBeTruthy();
      expect(nav).not.toHaveBeenCalled();
    });

    it('keeps the second-factor step a 401 names (UX-70)', async () => {
      vi.spyOn(router, 'navigate').mockResolvedValue(true);
      const result = firstValueFrom(http.get('/api/portfolio'));
      controller
        .expectOne('/api/portfolio')
        .flush({ ...problem(401, 'mfa_required: x'), next_step: 'verify' }, UNAUTHORIZED);
      await expect(result).rejects.toBeTruthy();
      expect(session.step()).toBe('verify');
    });

    it('bearer 401 clears the tab token and navigates to /login (UX-39)', async () => {
      const tokens = TestBed.inject(AuthTokenService);
      tokens.setToken('stk_revoked');
      const nav = vi.spyOn(router, 'navigate').mockResolvedValue(true);
      const result = firstValueFrom(http.get('/api/portfolio'));
      controller.expectOne('/api/portfolio').flush(problem(401, 'not_authenticated'), UNAUTHORIZED);
      await expect(result).rejects.toBeTruthy();
      expect(tokens.token()).toBeNull();
      expect(sessionStorage.getItem('stonks.apiToken')).toBeNull();
      expect(session.status()).toBe('signed-out');
      expect(nav).toHaveBeenCalledWith(['/login'], { queryParams: { next: '/' } });
    });

    it('lets the sign-in routes handle their own failures', async () => {
      await signIn();
      const nav = vi.spyOn(router, 'navigate');
      const result = firstValueFrom(http.post('/api/auth/mfa/verify', {}));
      controller
        .expectOne('/api/auth/mfa/verify')
        .flush(problem(401, 'invalid_credentials'), UNAUTHORIZED);
      await expect(result).rejects.toBeTruthy();
      expect(nav).not.toHaveBeenCalled();
      expect(session.status()).toBe('signed-in');
    });
  });

  describe('step-up', () => {
    it('asks for a code on step_up_required, then retries once', async () => {
      await signIn();
      const stepUp = TestBed.inject(StepUpService);
      const error = vi.spyOn(TestBed.inject(ToastService), 'error');
      const result = firstValueFrom(http.post('/api/auth/password', { a: 1 }));
      controller
        .expectOne('/api/auth/password')
        .flush(problem(403, 'step_up_required: password.change needs a fresh factor'), FORBIDDEN);
      await tick();
      expect(stepUp.request()).not.toBeNull();

      const submitted = stepUp.submit({ code: '123456' });
      (await nextRequest(controller, '/api/auth/mfa/verify', 'POST')).flush({
        method: 'totp',
        csrf_token: null,
        recovery_codes_left: 10,
      });
      await submitted;

      const retry = await nextRequest(controller, '/api/auth/password', 'POST');
      expect(retry.request.body).toEqual({ a: 1 });
      retry.flush(null, { status: 204, statusText: 'No Content' });
      await expect(result).resolves.toBeNull();
      expect(error).not.toHaveBeenCalled();
    });

    it('fails with the original error when the prompt is cancelled', async () => {
      await signIn();
      const stepUp = TestBed.inject(StepUpService);
      const error = vi.spyOn(TestBed.inject(ToastService), 'error');
      const result = firstValueFrom(http.post('/api/auth/password', {}));
      controller
        .expectOne('/api/auth/password')
        .flush(problem(403, 'step_up_required: x'), FORBIDDEN);
      await tick();
      stepUp.cancel();
      await expect(result).rejects.toMatchObject({ status: 403 });
      // The trader cancelled: no error toast on top (UX-38).
      expect(error).not.toHaveBeenCalled();
    });
  });
});
