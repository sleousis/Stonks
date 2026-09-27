import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { UserView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { ADMIN } from '../../../testing/auth-fixtures';
import { nextRequest, page, tick } from '../../../testing/http';
import { AdminUsersPage } from './admin-users.page';

const ANN: UserView = {
  id: 'usr_1',
  email: 'ann@example.com',
  display_name: 'Ann',
  role: 'trader',
  status: 'active',
  mfa_enrolled: true,
  created_at: '2026-09-01T10:00:00Z',
  last_login_at: null,
};

const ME: UserView = { ...ANN, id: ADMIN.user_id, display_name: 'Boss', role: 'admin' };

describe('AdminUsersPage', () => {
  let fixture: ComponentFixture<AdminUsersPage>;
  let controller: HttpTestingController;
  let confirm: ReturnType<typeof vi.spyOn>;

  beforeEach(async () => {
    TestBed.configureTestingModule({
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting()],
    });
    controller = TestBed.inject(HttpTestingController);
    confirm = vi.spyOn(TestBed.inject(ConfirmService), 'confirm').mockResolvedValue(true);
    const loading = TestBed.inject(SessionService).load();
    (await nextRequest(controller, '/api/auth/me')).flush(ADMIN);
    await loading;
  });

  afterEach(() => controller.verify());

  async function render(users: UserView[] = [ANN, ME]) {
    fixture = TestBed.createComponent(AdminUsersPage);
    fixture.detectChanges();
    (await nextRequest(controller, '/api/auth/users')).flush(page(users));
    await tick();
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  async function reload(users: UserView[]) {
    (await nextRequest(controller, '/api/auth/users')).flush(page(users));
    await tick();
    fixture.detectChanges();
  }

  function rowOf(el: HTMLElement, name: string) {
    return [...el.querySelectorAll('li')].find((li) => li.textContent?.includes(name))!;
  }

  function button(root: Element, text: string) {
    return [...root.querySelectorAll('button')].find((b) => b.textContent?.trim() === text)!;
  }

  function type(el: HTMLElement, selector: string, value: string) {
    const input = el.querySelector<HTMLInputElement>(selector)!;
    input.value = value;
    input.dispatchEvent(new Event('input'));
  }

  it('lists people by name and locks your own row', async () => {
    const el = await render();
    const names = [...el.querySelectorAll('.name')].map((n) => n.textContent?.trim());
    expect(names[0]).toContain('Ann');
    expect(names[1]).toContain('Boss');
    expect(el.textContent).toContain('Never signed in');
    expect(button(rowOf(el, 'Boss'), 'Disable').disabled).toBe(true);
    expect(button(rowOf(el, 'Ann'), 'Disable').disabled).toBe(false);
  });

  it('adds a person', async () => {
    const el = await render();
    button(el, 'Add person').click();
    fixture.detectChanges();
    type(el, '#user-name', 'Cal');
    type(el, '#user-email', ' cal@example.com ');
    type(el, '#user-password', 'a long first password');
    el.querySelector<HTMLFormElement>('#add-user form')!.dispatchEvent(new Event('submit'));
    const req = await nextRequest(controller, '/api/auth/users', 'POST');
    expect(req.request.body).toEqual({
      display_name: 'Cal',
      email: 'cal@example.com',
      role: 'trader',
      password: 'a long first password',
    });
    req.flush({ ...ANN, id: 'usr_3', display_name: 'Cal', mfa_enrolled: false });
    await reload([ANN, ME]);
  });

  it('both password inputs are autocomplete=off with a reveal toggle (UX-40)', async () => {
    const el = await render();
    button(el, 'Add person').click();
    fixture.detectChanges();
    const first = el.querySelector<HTMLInputElement>('#user-password')!;
    expect(first.getAttribute('autocomplete')).toBe('off');
    expect(first.hasAttribute('data-1p-ignore')).toBe(true);
    expect(first.getAttribute('data-lpignore')).toBe('true');
    expect(first.type).toBe('password');
    button(first.parentElement!, 'Show').click();
    fixture.detectChanges();
    expect(first.type).toBe('text');

    button(rowOf(el, 'Ann'), 'Reset password').click();
    fixture.detectChanges();
    const reset = el.querySelector<HTMLInputElement>('#reset-password')!;
    expect(reset.getAttribute('autocomplete')).toBe('off');
    expect(reset.hasAttribute('data-1p-ignore')).toBe(true);
    button(reset.parentElement!, 'Show').click();
    fixture.detectChanges();
    expect(reset.type).toBe('text');
  });

  it('generates a strong first password, shown and ready to copy (UX-40)', async () => {
    const el = await render();
    button(el, 'Add person').click();
    fixture.detectChanges();
    button(el, 'Generate').click();
    fixture.detectChanges();
    const input = el.querySelector<HTMLInputElement>('#user-password')!;
    expect(input.value).toMatch(/^([A-Za-z2-9]{5}-){3}[A-Za-z2-9]{5}$/);
    expect(input.type).toBe('text');
    expect(el.querySelector('#add-user app-copy-button')).not.toBeNull();
  });

  it('checks the form before sending', async () => {
    const el = await render();
    button(el, 'Add person').click();
    fixture.detectChanges();
    type(el, '#user-password', 'short');
    el.querySelector<HTMLFormElement>('#add-user form')!.dispatchEvent(new Event('submit'));
    fixture.detectChanges();
    expect(el.textContent).toContain('Enter a name.');
    expect(el.textContent).toContain('Use at least 12 characters.');
    controller.expectNone({ method: 'POST', url: '/api/auth/users' });
  });

  it('changes a role after asking', async () => {
    const el = await render();
    const select = rowOf(el, 'Ann').querySelector<HTMLSelectElement>('select')!;
    select.value = 'admin';
    select.dispatchEvent(new Event('change'));
    const req = await nextRequest(controller, '/api/auth/users/usr_1', 'PATCH');
    expect(confirm).toHaveBeenCalledWith(expect.objectContaining({ title: 'Make Ann an admin?' }));
    expect(req.request.body).toEqual({ role: 'admin' });
    req.flush({ ...ANN, role: 'admin' });
    await reload([{ ...ANN, role: 'admin' }, ME]);
  });

  it('puts the role back when the change is cancelled', async () => {
    confirm.mockResolvedValue(false);
    const el = await render();
    const select = rowOf(el, 'Ann').querySelector<HTMLSelectElement>('select')!;
    select.value = 'viewer';
    select.dispatchEvent(new Event('change'));
    await tick();
    expect(select.value).toBe('trader');
    controller.expectNone('/api/auth/users/usr_1');
  });

  it('disables a person', async () => {
    const el = await render();
    button(rowOf(el, 'Ann'), 'Disable').click();
    const req = await nextRequest(controller, '/api/auth/users/usr_1', 'PATCH');
    expect(confirm).toHaveBeenCalledWith(expect.objectContaining({ tone: 'danger' }));
    expect(req.request.body).toEqual({ status: 'disabled' });
    req.flush({ ...ANN, status: 'disabled' });
    await reload([{ ...ANN, status: 'disabled' }, ME]);
    expect(button(rowOf(el, 'Ann'), 'Enable')).toBeTruthy();
  });

  it('resets the authenticator', async () => {
    const el = await render();
    button(rowOf(el, 'Ann'), 'Reset app').click();
    (await nextRequest(controller, '/api/auth/users/usr_1/mfa', 'DELETE')).flush(null, {
      status: 204,
      statusText: 'No Content',
    });
    await reload([{ ...ANN, mfa_enrolled: false }, ME]);
    expect(rowOf(el, 'Ann').textContent).toContain('App not set up');
  });

  it('resets a password after the email is typed', async () => {
    const el = await render();
    button(rowOf(el, 'Ann'), 'Reset password').click();
    fixture.detectChanges();
    const sheet = el.querySelector('#reset-password-form')!;
    expect(sheet.textContent).toContain('signed out');
    expect(sheet.textContent).toContain('API tokens stop working');
    const submit = button(sheet, 'Reset password');
    type(el, '#reset-password', 'short');
    fixture.detectChanges();
    expect(submit.disabled).toBe(true);
    type(el, '#reset-password', 'a brand new passphrase');
    type(el, '#reset-typed', 'ann@example.com');
    fixture.detectChanges();
    expect(submit.disabled).toBe(false);
    submit.click();
    const req = await nextRequest(controller, '/api/auth/users/usr_1/password', 'POST');
    expect(req.request.body).toEqual({ new_password: 'a brand new passphrase' });
    req.flush(null, { status: 204, statusText: 'No Content' });
    await tick();
    fixture.detectChanges();
    expect(el.querySelector('#reset-password-form')).toBeNull();
  });
});
