import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import type { WritableSignal } from '@angular/core';
import { Router, provideRouter } from '@angular/router';
import { SwPush } from '@angular/service-worker';
import { of } from 'rxjs';

import { provideApi } from '../../api/provide-api';
import { AuthTokenService } from '../../core/auth/auth-token.service';
import { DEVICE_INFO, NOTIFICATION_API } from '../../core/pwa/notification-permission.service';
import { PUSH_SUBSCRIPTION_API } from '../../core/pwa/push-subscription-api';
import { TRADER, UNAUTHORIZED, problem } from '../../../testing/auth-fixtures';
import { nextRequest, tick } from '../../../testing/http';
import { LoginPage } from './login.page';

describe('LoginPage', () => {
  let fixture: ComponentFixture<LoginPage>;
  let controller: HttpTestingController;
  let navigate: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    sessionStorage.clear();
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        ...provideApi(),
        provideHttpClientTesting(),
        {
          provide: NOTIFICATION_API,
          useValue: { permission: 'default', requestPermission: async () => 'default' },
        },
        { provide: DEVICE_INFO, useValue: { ios: false, standalone: false, userAgent: 'test' } },
      ],
    });
    controller = TestBed.inject(HttpTestingController);
    navigate = vi.spyOn(TestBed.inject(Router), 'navigateByUrl').mockResolvedValue(true);
  });

  afterEach(() => controller.verify());

  function render(inputs: { step?: string; next?: string } = {}) {
    fixture = TestBed.createComponent(LoginPage);
    if (inputs.step) fixture.componentRef.setInput('step', inputs.step);
    if (inputs.next) fixture.componentRef.setInput('next', inputs.next);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  function type(el: HTMLElement, selector: string, value: string) {
    const input = el.querySelector<HTMLInputElement>(selector)!;
    input.value = value;
    input.dispatchEvent(new Event('input'));
  }

  function submit(el: HTMLElement) {
    el.querySelector('form')!.dispatchEvent(new Event('submit'));
  }

  async function settle() {
    await tick();
    fixture.detectChanges();
  }

  function heading(el: HTMLElement) {
    return el.querySelector('h1')!.textContent?.trim();
  }

  it('signs in with a password and a code, then goes where it was heading', async () => {
    const el = render({ next: '/lab' });
    expect(heading(el)).toBe('Sign in');
    type(el, '#login-email', ' ann@example.com ');
    type(el, '#login-password', 'secret');
    submit(el);

    const login = await nextRequest(controller, '/api/auth/login', 'POST');
    expect(login.request.body).toEqual({ email: 'ann@example.com', password: 'secret' });
    login.flush({ user_id: 'usr_1', display_name: 'Ann', next_step: 'verify', csrf_token: 'c' });
    await settle();
    expect(heading(el)).toBe('Enter your code');

    type(el, '#verify-code', '123 456');
    submit(el);
    const verify = await nextRequest(controller, '/api/auth/mfa/verify', 'POST');
    expect(verify.request.body).toEqual({ code: '123456' });
    verify.flush({ method: 'totp', csrf_token: 'c2', recovery_codes_left: 10 });
    (await nextRequest(controller, '/api/auth/me')).flush(TRADER);
    await settle();
    expect(navigate).toHaveBeenCalledWith('/lab');
  });

  it('shows a wrong password inline', async () => {
    const el = render();
    type(el, '#login-email', 'ann@example.com');
    type(el, '#login-password', 'nope');
    submit(el);
    (await nextRequest(controller, '/api/auth/login', 'POST')).flush(
      problem(401, 'invalid_credentials'),
      UNAUTHORIZED,
    );
    await settle();
    expect(el.querySelector('[role="alert"]')?.textContent).toContain('did not match');
    expect(heading(el)).toBe('Sign in');
  });

  it('asks for both fields before sending anything', async () => {
    const el = render();
    submit(el);
    await settle();
    expect(el.textContent).toContain('Enter your email.');
    expect(el.textContent).toContain('Enter your password.');
  });

  it('first login: QR code, confirm, recovery codes once, then the push offer', async () => {
    const el = render();
    type(el, '#login-email', 'ann@example.com');
    type(el, '#login-password', 'secret');
    submit(el);
    (await nextRequest(controller, '/api/auth/login', 'POST')).flush({
      user_id: 'usr_1',
      display_name: 'Ann',
      next_step: 'enrol',
      csrf_token: 'c',
    });
    (await nextRequest(controller, '/api/auth/mfa/enrol', 'POST')).flush({
      secret: 'JBSWY3DPEHPK3PXP',
      otpauth_uri: 'otpauth://totp/Stonks:ann?secret=JBSWY3DPEHPK3PXP&issuer=Stonks',
    });
    await settle();
    expect(heading(el)).toBe('Set up your authenticator');
    expect(el.querySelector('app-qr-code svg path')?.getAttribute('d')).toMatch(/^M/);
    expect(el.textContent).toContain('JBSW Y3DP EHPK 3PXP');
    // UX-11: a phone cannot scan its own screen.
    const link = el.querySelector<HTMLAnchorElement>('a.app-link')!;
    expect(link.getAttribute('href')).toBe(
      'otpauth://totp/Stonks:ann?secret=JBSWY3DPEHPK3PXP&issuer=Stonks',
    );
    expect(link.textContent?.trim()).toBe('Add to authenticator app');
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    el.querySelector<HTMLButtonElement>('app-copy-button button')!.click();
    await settle();
    expect(writeText).toHaveBeenCalledWith('JBSWY3DPEHPK3PXP');

    type(el, '#enrol-code', '123456');
    submit(el);
    (await nextRequest(controller, '/api/auth/mfa/enrol/confirm', 'POST')).flush({
      method: 'totp',
      csrf_token: 'c2',
      recovery_codes: ['aaaa-1111', 'bbbb-2222'],
      recovery_codes_left: 10,
    });
    (await nextRequest(controller, '/api/auth/me')).flush(TRADER);
    await settle();
    expect(heading(el)).toBe('Save your recovery codes');
    expect(el.textContent).toContain('aaaa-1111');

    const cont = [...el.querySelectorAll('button')].find(
      (b) => b.textContent?.trim() === 'Continue',
    )!;
    expect(cont.disabled).toBe(true);
    el.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click();
    fixture.detectChanges();
    expect(cont.disabled).toBe(false);
    cont.click();
    await settle();
    expect(heading(el)).toBe('Get alerts on this device?');

    [...el.querySelectorAll('button')].find((b) => b.textContent?.trim() === 'Not now')!.click();
    await settle();
    expect(navigate).toHaveBeenCalledWith('/');
  });

  it('after a reload mid sign-in, asks for the code when an app is already set up', async () => {
    const el = render({ step: 'code' });
    (await nextRequest(controller, '/api/auth/mfa/enrol', 'POST')).flush(
      problem(409, 'a second factor is already set up'),
      { status: 409, statusText: 'Conflict' },
    );
    await settle();
    expect(heading(el)).toBe('Enter your code');
  });

  it('accepts a recovery code', async () => {
    const el = render({ step: 'code' });
    (await nextRequest(controller, '/api/auth/mfa/enrol', 'POST')).flush(problem(409, 'x'), {
      status: 409,
      statusText: 'Conflict',
    });
    await settle();
    [...el.querySelectorAll('button')]
      .find((b) => b.textContent?.includes('recovery code'))!
      .click();
    fixture.detectChanges();
    type(el, '#verify-recovery', 'aaaa-1111');
    submit(el);
    const verify = await nextRequest(controller, '/api/auth/mfa/verify', 'POST');
    expect(verify.request.body).toEqual({ recovery_code: 'aaaa-1111' });
    verify.flush({ method: 'recovery_code', csrf_token: 'c', recovery_codes_left: 9 });
    (await nextRequest(controller, '/api/auth/me')).flush(TRADER);
    await settle();
    expect(navigate).toHaveBeenCalledWith('/');
  });

  it('keeps token mode working: a valid API token signs in for this tab', async () => {
    const el = render();
    el.querySelector('details')!.open = true;
    type(el, '#login-token', 'stk_abc');
    el.querySelector<HTMLFormElement>('#token-form')!.dispatchEvent(new Event('submit'));
    const me = await nextRequest(controller, '/api/auth/me');
    expect(me.request.headers.get('Authorization')).toBe('Bearer stk_abc');
    me.flush({ ...TRADER, via: 'token' });
    await settle();
    expect(navigate).toHaveBeenCalledWith('/');
    expect(TestBed.inject(AuthTokenService).token()).toBe('stk_abc');
  });

  it('drops a token the API rejects', async () => {
    const el = render();
    el.querySelector('details')!.open = true;
    type(el, '#login-token', 'wrong');
    el.querySelector<HTMLFormElement>('#token-form')!.dispatchEvent(new Event('submit'));
    (await nextRequest(controller, '/api/auth/me')).flush(
      problem(401, 'not_authenticated'),
      UNAUTHORIZED,
    );
    await settle();
    expect(el.querySelector('[role="alert"]')?.textContent).toContain('token did not work');
    expect(TestBed.inject(AuthTokenService).token()).toBeNull();
  });

  it('keeps the API-token path folded under For scripts (UX-43)', () => {
    const el = render();
    const details = el.querySelector('details')!;
    expect(details.querySelector('summary')?.textContent?.trim()).toBe('For scripts');
    expect(details.open).toBe(false);
  });

  it('shows a wait, not the password form, on a reload mid sign-in (UX-70)', async () => {
    const el = render({ step: 'code' });
    expect(heading(el)).toBe('Signing in');
    expect(el.querySelector('[role="status"]')?.textContent?.trim()).toBe('One moment…');
    expect(el.querySelector('#login-password')).toBeNull();
    (await nextRequest(controller, '/api/auth/mfa/enrol', 'POST')).flush(problem(409, 'x'), {
      status: 409,
      statusText: 'Conflict',
    });
    await settle();
    expect(heading(el)).toBe('Enter your code');
  });

  it('with no VAPID key the push step stays and explains (UX-46)', async () => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        ...provideApi(),
        provideHttpClientTesting(),
        {
          provide: NOTIFICATION_API,
          useValue: { permission: 'default', requestPermission: async () => 'granted' },
        },
        { provide: DEVICE_INFO, useValue: { ios: false, standalone: false, userAgent: 'test' } },
        { provide: SwPush, useValue: { isEnabled: true, subscription: of(null) } },
        {
          provide: PUSH_SUBSCRIPTION_API,
          useValue: { vapidPublicKey: async () => null, save: vi.fn(), remove: vi.fn() },
        },
      ],
    });
    controller = TestBed.inject(HttpTestingController);
    navigate = vi.spyOn(TestBed.inject(Router), 'navigateByUrl').mockResolvedValue(true);
    const el = render();
    (fixture.componentInstance as unknown as { current: WritableSignal<string> }).current.set(
      'push',
    );
    fixture.detectChanges();
    [...el.querySelectorAll('button')]
      .find((b) => b.textContent?.includes('Turn on alerts'))!
      .click();
    await settle();
    await settle();
    expect(heading(el)).toBe('Get alerts on this device?');
    expect(el.textContent).toContain('This server cannot send alerts yet');
    expect(navigate).not.toHaveBeenCalled();
    [...el.querySelectorAll('button')].find((b) => b.textContent?.trim() === 'Continue')!.click();
    await settle();
    expect(navigate).toHaveBeenCalledWith('/');
  });
});
