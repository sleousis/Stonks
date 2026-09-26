import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { provideApi } from '../../api/provide-api';
import { TRADER, UNAUTHORIZED } from '../../../testing/auth-fixtures';
import { nextRequest } from '../../../testing/http';
import { AuthTokenService } from './auth-token.service';
import { SessionService } from './session.service';

describe('SessionService', () => {
  let session: SessionService;
  let controller: HttpTestingController;

  beforeEach(() => {
    sessionStorage.clear();
    document.cookie = 'stonks_csrf=; Max-Age=0; path=/';
    TestBed.configureTestingModule({
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting()],
    });
    session = TestBed.inject(SessionService);
    controller = TestBed.inject(HttpTestingController);
  });

  afterEach(() => controller.verify());

  it('is signed in when /me answers', async () => {
    const loading = session.load();
    (await nextRequest(controller, '/api/auth/me')).flush(TRADER);
    expect(await loading).toBe('signed-in');
    expect(session.me()?.display_name).toBe('Ann');
    expect(session.isAdmin()).toBe(false);
    expect(session.canTrade()).toBe(true);
    expect(session.viaSession()).toBe(true);
  });

  it('shares one request between concurrent loads and caches the answer', async () => {
    const a = session.load();
    const b = session.load();
    (await nextRequest(controller, '/api/auth/me')).flush(TRADER);
    expect(await a).toBe('signed-in');
    expect(await b).toBe('signed-in');
    expect(await session.load()).toBe('signed-in');
  });

  it('is waiting for the second factor on mfa_required', async () => {
    const loading = session.load();
    (await nextRequest(controller, '/api/auth/me')).flush(
      { title: 'Unauthorized', status: 401, detail: 'mfa_required: finish signing in' },
      UNAUTHORIZED,
    );
    expect(await loading).toBe('mfa-pending');
  });

  it('is open when /me says reads work without a credential (dev profile)', async () => {
    const loading = session.load();
    (await nextRequest(controller, '/api/auth/me')).flush(
      {
        title: 'Unauthorized',
        status: 401,
        detail: 'reads_open: nobody is signed in',
        code: 'reads_open',
      },
      UNAUTHORIZED,
    );
    expect(await loading).toBe('open');
    expect(session.me()).toBeNull();
    expect(session.canRead()).toBe(true);
  });

  it('is signed out on a plain 401, without probing a data route (BUG-1)', async () => {
    const loading = session.load();
    (await nextRequest(controller, '/api/auth/me')).flush(
      {
        title: 'Unauthorized',
        status: 401,
        detail: 'not_authenticated',
        code: 'not_authenticated',
      },
      UNAUTHORIZED,
    );
    expect(await loading).toBe('signed-out');
    expect(session.canRead()).toBe(false);
    // afterEach's verify() fails on any other request, such as /api/strategies
  });

  it('may read only once signed in or open', async () => {
    expect(session.canRead()).toBe(false);
    const loading = session.load();
    (await nextRequest(controller, '/api/auth/me')).flush(TRADER);
    await loading;
    expect(session.canRead()).toBe(true);
  });

  it('asks /me with the saved API token (token mode)', async () => {
    TestBed.inject(AuthTokenService).setToken('stk_abc');
    const loading = session.load();
    const req = await nextRequest(controller, '/api/auth/me');
    expect(req.request.headers.get('Authorization')).toBe('Bearer stk_abc');
    req.flush({ ...TRADER, via: 'token' });
    expect(await loading).toBe('signed-in');
    expect(session.viaSession()).toBe(false);
  });

  it('signs in: password, then the code, keeping the CSRF token', async () => {
    const step = session.login('ann@example.com', 'pw');
    const login = await nextRequest(controller, '/api/auth/login', 'POST');
    expect(login.request.body).toEqual({ email: 'ann@example.com', password: 'pw' });
    login.flush({ user_id: 'usr_1', display_name: 'Ann', next_step: 'verify', csrf_token: 'c1' });
    expect(await step).toBe('verify');
    expect(session.status()).toBe('mfa-pending');
    expect(session.csrfToken()).toBe('c1');

    const verified = session.verify({ code: '123456' });
    (await nextRequest(controller, '/api/auth/mfa/verify', 'POST')).flush({
      method: 'totp',
      csrf_token: 'c2',
      recovery_codes_left: 9,
    });
    (await nextRequest(controller, '/api/auth/me')).flush({ ...TRADER, mfa_fresh: true });
    await verified;
    expect(session.status()).toBe('signed-in');
    expect(session.csrfToken()).toBe('c2');
  });

  it('confirms enrolment and returns the recovery codes', async () => {
    const codes = session.confirmEnrolment('123456');
    (await nextRequest(controller, '/api/auth/mfa/enrol/confirm', 'POST')).flush({
      method: 'totp',
      csrf_token: 'c3',
      recovery_codes: ['a-1', 'b-2'],
      recovery_codes_left: 10,
    });
    (await nextRequest(controller, '/api/auth/me')).flush(TRADER);
    expect(await codes).toEqual(['a-1', 'b-2']);
    expect(session.justEnrolled()).toBe(true);
  });

  it('marks the step-up fresh after a verify on a full session', async () => {
    const loading = session.load();
    (await nextRequest(controller, '/api/auth/me')).flush(TRADER);
    await loading;
    const verified = session.verify({ code: '654321' });
    (await nextRequest(controller, '/api/auth/mfa/verify', 'POST')).flush({
      method: 'totp',
      csrf_token: null,
      recovery_codes_left: 10,
    });
    await verified;
    expect(session.me()?.mfa_fresh).toBe(true);
  });

  it('reads the CSRF token from its cookie after a reload', () => {
    document.cookie = 'stonks_csrf=fromcookie; path=/';
    expect(session.csrfToken()).toBe('fromcookie');
  });

  it('logs out and forgets the user and the tab token', async () => {
    TestBed.inject(AuthTokenService).setToken('stk_abc');
    const loading = session.load();
    (await nextRequest(controller, '/api/auth/me')).flush(TRADER);
    await loading;
    const out = session.logout();
    (await nextRequest(controller, '/api/auth/logout', 'POST')).flush(null, {
      status: 204,
      statusText: 'No Content',
    });
    await out;
    expect(session.status()).toBe('signed-out');
    expect(session.me()).toBeNull();
    expect(TestBed.inject(AuthTokenService).token()).toBeNull();
  });

  describe('permissions (UI-06)', () => {
    it('knows nothing is allowed before /me answers', () => {
      expect(session.can('lab.run')).toBe(false);
      expect(session.whyNot('lab.run')).toBe('Sign in to do this.');
    });

    it('answers can() and canCall() from /me', async () => {
      const loading = session.load();
      (await nextRequest(controller, '/api/auth/me')).flush(TRADER);
      await loading;
      expect(session.can('lab.run')).toBe(true);
      expect(session.can('strategy.promote')).toBe(false);
      expect(session.whyNot('strategy.promote')).toBe('Admins only.');
      expect(session.canCall('POST', '/api/ticks')).toBe(false);
      expect(session.canCall('POST', '/api/lab/backtests')).toBe(true);
      expect(session.canCall('GET', '/api/portfolio')).toBe(true);
    });
  });
});
