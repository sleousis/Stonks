import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { MeView, TokenView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { HARD_NAVIGATE } from '../../core/auth/hard-navigate';
import { SessionService } from '../../core/auth/session.service';
import { StepUpService } from '../../core/auth/step-up.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { FORBIDDEN, TRADER, problem } from '../../../testing/auth-fixtures';
import { nextRequest, page, tick } from '../../../testing/http';
import { ProfilePage } from './profile.page';

const TOKEN: TokenView = {
  id: 'tok_1',
  name: 'laptop',
  scopes: ['read'],
  created_at: '2026-09-01T10:00:00Z',
  last_used_at: null,
  expires_at: null,
  revoked_at: null,
};

describe('ProfilePage', () => {
  let fixture: ComponentFixture<ProfilePage>;
  let controller: HttpTestingController;
  let hardNavigate: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    hardNavigate = vi.fn();
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        ...provideApi(),
        provideHttpClientTesting(),
        { provide: HARD_NAVIGATE, useValue: hardNavigate },
      ],
    });
    controller = TestBed.inject(HttpTestingController);
  });

  afterEach(() => controller.verify());

  async function render(me: MeView = TRADER, tokens: TokenView[] = [TOKEN]) {
    const session = TestBed.inject(SessionService);
    const loading = session.load();
    (await nextRequest(controller, '/api/auth/me')).flush(me);
    await loading;
    fixture = TestBed.createComponent(ProfilePage);
    fixture.detectChanges();
    (await nextRequest(controller, '/api/auth/tokens')).flush(page(tokens));
    (await nextRequest(controller, '/api/portfolios')).flush(page([]));
    await tick();
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  function type(el: HTMLElement, selector: string, value: string) {
    const input = el.querySelector<HTMLInputElement>(selector)!;
    input.value = value;
    input.dispatchEvent(new Event('input'));
  }

  function button(el: HTMLElement, text: string) {
    return [...el.querySelectorAll('button')].find((b) => b.textContent?.trim().startsWith(text))!;
  }

  it('shows the account and the tokens', async () => {
    const el = await render();
    expect(el.textContent).toContain('ann@example.com');
    expect(el.textContent).toContain('Trader');
    expect(el.textContent).toContain('laptop');
    expect(el.textContent).toContain('never used');
    expect(el.textContent).toContain('For scripts and assistant tools.');
    expect(el.textContent).not.toContain('MCP');
  });

  it('checks the new password before sending', async () => {
    const el = await render();
    type(el, '#pw-current', 'old-password');
    type(el, '#pw-next', 'short');
    type(el, '#pw-repeat', 'other');
    el.querySelector<HTMLFormElement>('form')!.dispatchEvent(new Event('submit'));
    fixture.detectChanges();
    expect(el.textContent).toContain('Use at least 12 characters.');
    expect(el.textContent).toContain('The two passwords differ.');
    controller.expectNone('/api/auth/password');
    // UX-61: each invalid field points at the error it shows.
    for (const id of ['pw-next', 'pw-repeat']) {
      const input = el.querySelector<HTMLInputElement>('#' + id)!;
      expect(input.getAttribute('aria-invalid'), id).toBe('true');
      const described = el.querySelector('#' + input.getAttribute('aria-describedby'));
      expect(described?.classList.contains('error'), id).toBe(true);
    }
  });

  it('changes the password, asking for a code when the API wants one', async () => {
    const el = await render();
    const stepUp = TestBed.inject(StepUpService);
    type(el, '#pw-current', 'old-password-123');
    type(el, '#pw-next', 'a much longer phrase');
    type(el, '#pw-repeat', 'a much longer phrase');
    el.querySelector<HTMLFormElement>('form')!.dispatchEvent(new Event('submit'));

    (await nextRequest(controller, '/api/auth/password', 'POST')).flush(
      problem(403, 'step_up_required: password.change needs a fresh second factor'),
      FORBIDDEN,
    );
    await vi.waitFor(() => expect(stepUp.request()).not.toBeNull());
    const submitted = stepUp.submit({ code: '123456' });
    (await nextRequest(controller, '/api/auth/mfa/verify', 'POST')).flush({
      method: 'totp',
      csrf_token: null,
      recovery_codes_left: 10,
    });
    await submitted;
    const retry = await nextRequest(controller, '/api/auth/password', 'POST');
    expect(retry.request.body).toEqual({
      current_password: 'old-password-123',
      new_password: 'a much longer phrase',
    });
    retry.flush(null, { status: 204, statusText: 'No Content' });
    await tick();
  });

  it('makes new recovery codes and shows them once', async () => {
    vi.spyOn(TestBed.inject(ConfirmService), 'confirm').mockResolvedValue(true);
    const el = await render();
    button(el, 'Make new codes').click();
    (await nextRequest(controller, '/api/auth/recovery-codes', 'POST')).flush({
      recovery_codes: ['aaaa-1111', 'bbbb-2222'],
    });
    await tick();
    fixture.detectChanges();
    expect(el.textContent).toContain('aaaa-1111');
    expect(el.textContent).toContain('You cannot see these again.');
  });

  it('creates a token and shows it once', async () => {
    const el = await render();
    type(el, '#token-name', 'ci');
    el.querySelector('#token-name')!.closest('form')!.dispatchEvent(new Event('submit'));
    const req = await nextRequest(controller, '/api/auth/tokens', 'POST');
    expect(req.request.body).toEqual({
      name: 'ci',
      scopes: ['read'],
      expires_in_days: 90,
      toolsets: null,
    });
    req.flush({ token: 'stk_new_secret', info: { ...TOKEN, id: 'tok_2', name: 'ci' } });
    (await nextRequest(controller, '/api/auth/tokens')).flush(page([TOKEN]));
    await tick();
    fixture.detectChanges();
    expect(el.querySelector('.single')?.textContent).toBe('stk_new_secret');
  });

  it('limits a token to the MCP tool groups you pick', async () => {
    const el = await render();
    type(el, '#token-name', 'mcp');
    const details = el.querySelector('details.toolsets') as HTMLDetailsElement;
    details.open = true;
    details.dispatchEvent(new Event('toggle'));
    (await nextRequest(controller, '/api/auth/toolsets')).flush(
      page([
        { name: 'risk', about: 'Live risk monitoring tools.' },
        { name: 'decisions', about: "Why did or didn't we trade." },
      ]),
    );
    await tick();
    fixture.detectChanges();
    const boxes = details.querySelectorAll<HTMLInputElement>('input[type=checkbox]');
    boxes[1].checked = true;
    boxes[1].dispatchEvent(new Event('change'));
    el.querySelector('#token-name')!.closest('form')!.dispatchEvent(new Event('submit'));
    const req = await nextRequest(controller, '/api/auth/tokens', 'POST');
    expect(req.request.body.toolsets).toEqual(['decisions']);
    req.flush({ token: 'stk_x', info: { ...TOKEN, id: 'tok_3', name: 'mcp' } });
    (await nextRequest(controller, '/api/auth/tokens')).flush(page([TOKEN]));
    await tick();
  });

  it('asks for at least one scope', async () => {
    const el = await render();
    type(el, '#token-name', 'ci');
    const read = el.querySelector<HTMLInputElement>('input[type="checkbox"]')!;
    read.click();
    el.querySelector('#token-name')!.closest('form')!.dispatchEvent(new Event('submit'));
    fixture.detectChanges();
    expect(el.textContent).toContain('Pick at least one scope.');
    controller.expectNone({ method: 'POST', url: '/api/auth/tokens' });
  });

  it('revokes a token after asking', async () => {
    vi.spyOn(TestBed.inject(ConfirmService), 'confirm').mockResolvedValue(true);
    const el = await render();
    button(el, 'Revoke').click();
    (await nextRequest(controller, '/api/auth/tokens/tok_1', 'DELETE')).flush(null, {
      status: 204,
      statusText: 'No Content',
    });
    (await nextRequest(controller, '/api/auth/tokens')).flush(page([]));
    await tick();
    fixture.detectChanges();
    expect(el.textContent).toContain('No tokens');
  });

  it('in token mode, hides password and token creation', async () => {
    const el = await render({ ...TRADER, via: 'token' });
    expect(el.querySelector('#pw-current')).toBeNull();
    expect(el.textContent).toContain('Sign in with your password to create tokens.');
  });

  it('signs out and reloads on the sign-in page (UX-07)', async () => {
    const el = await render();
    button(el, 'Sign out').click();
    (await nextRequest(controller, '/api/auth/logout', 'POST')).flush(null, {
      status: 204,
      statusText: 'No Content',
    });
    await tick();
    expect(hardNavigate).toHaveBeenCalledWith('/login');
  });
});
