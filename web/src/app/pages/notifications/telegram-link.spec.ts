import type { Mock } from 'vitest';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';

import type { TelegramLinkView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { type ConfirmOptions, ConfirmService } from '../../core/confirm/confirm.service';
import { nextRequest, tick } from '../../../testing/http';
import { TelegramLink, botLink } from './telegram-link';

function link(over: Partial<TelegramLinkView> = {}): TelegramLinkView {
  return {
    bot_configured: true,
    bot_enabled: true,
    bot_username: 'stonks_bot',
    linked: false,
    linked_at: null,
    username: null,
    ...over,
  };
}

describe('TelegramLink', () => {
  let fixture: ComponentFixture<TelegramLink>;
  let http: HttpTestingController;
  let confirm: Mock<(o: ConfirmOptions) => Promise<boolean>>;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [...provideApi(), provideHttpClientTesting()] });
    http = TestBed.inject(HttpTestingController);
    const session = TestBed.inject(SessionService);
    vi.spyOn(session, 'can').mockReturnValue(true);
    vi.spyOn(session, 'whyNot').mockReturnValue(null);
    confirm = vi.fn<(o: ConfirmOptions) => Promise<boolean>>(async () => true);
    vi.spyOn(TestBed.inject(ConfirmService), 'confirm').mockImplementation(confirm);
  });

  afterEach(() => http.verify());

  async function settle() {
    for (let i = 0; i < 3; i++) {
      await tick();
      fixture.detectChanges();
    }
  }

  async function render(view: TelegramLinkView) {
    fixture = TestBed.createComponent(TelegramLink);
    fixture.detectChanges();
    (await nextRequest(http, '/api/telegram/link')).flush(view);
    await settle();
    return fixture.nativeElement as HTMLElement;
  }

  const button = (el: HTMLElement, text: string) =>
    [...el.querySelectorAll<HTMLButtonElement>('button')].find(
      (b) => b.textContent?.trim() === text,
    )!;

  it('says when the server has no bot', async () => {
    const el = await render(link({ bot_configured: false }));
    expect(el.textContent).toContain('no Telegram bot yet');
    expect(button(el, 'Get a link code')).toBeUndefined();
    // Only admins get the next step (F40).
    expect(el.textContent).not.toContain('operations guide');
  });

  it('gives an admin the next step when the server has no bot (F40)', async () => {
    vi.spyOn(TestBed.inject(SessionService), 'isAdmin').mockReturnValue(true);
    const el = await render(link({ bot_configured: false }));
    expect(el.textContent).toContain('operations guide');
    expect(el.textContent).toContain('BotFather');
  });

  it('shows a one-time code with the bot to send it to, then checks the link', async () => {
    const el = await render(link());
    expect(el.textContent).toContain('Not linked');
    button(el, 'Get a link code').click();
    (await nextRequest(http, '/api/telegram/link-code', 'POST')).flush({
      code: 'K7Q2ZP',
      expires_at: '2026-09-27T10:10:00Z',
      bot_username: 'stonks_bot',
    });
    await settle();
    expect(el.querySelector('app-one-time-secret code')?.textContent).toBe('/link K7Q2ZP');
    expect(el.textContent).toContain('Shown once');
    expect(el.querySelector('a[href="https://t.me/stonks_bot"]')).not.toBeNull();
    button(el, 'Check the link').click();
    (await nextRequest(http, '/api/telegram/link')).flush(
      link({ linked: true, username: 'tess', linked_at: '2026-09-27T10:02:00Z' }),
    );
    await settle();
    expect(el.textContent).toContain('@tess');
    expect(el.textContent).toContain('2026-09-27');
    expect(el.querySelector('app-one-time-secret')).toBeNull();
  });

  it('asks first, then unlinks', async () => {
    const el = await render(link({ linked: true, username: 'tess' }));
    button(el, 'Unlink Telegram').click();
    const req = await nextRequest(http, '/api/telegram/link', 'DELETE');
    expect(confirm).toHaveBeenCalledWith(expect.objectContaining({ tone: 'danger' }));
    req.flush(null, { status: 204, statusText: 'No Content' });
    (await nextRequest(http, '/api/telegram/link')).flush(link());
    await settle();
    expect(el.textContent).toContain('Not linked');
  });

  it('says the bot does not answer when commands are off', async () => {
    const el = await render(link({ bot_enabled: false }));
    expect(el.textContent).toContain('does not answer messages');
  });

  it('builds the bot link only from a real name', () => {
    expect(botLink('@stonks_bot')).toBe('https://t.me/stonks_bot');
    expect(botLink('bad name')).toBeNull();
    expect(botLink(null)).toBeNull();
  });
});
