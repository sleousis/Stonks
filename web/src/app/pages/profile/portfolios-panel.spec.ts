import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { nextRequest, page, tick } from '../../../testing/http';
import { book } from '../../../testing/portfolio-fixtures';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { PortfoliosPanel } from './portfolios-panel';

const MAIN = book({ id: 'pf_1', name: 'Main', is_default: true });
const LIVE = book({ id: 'pf_2', name: 'Broker', kind: 'broker', trading: 'live' });

describe('PortfoliosPanel', () => {
  let fixture: ComponentFixture<PortfoliosPanel>;
  let controller: HttpTestingController;
  let allowed: boolean;

  beforeEach(() => {
    allowed = true;
    TestBed.configureTestingModule({
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting()],
    });
    controller = TestBed.inject(HttpTestingController);
    const session = TestBed.inject(SessionService);
    vi.spyOn(session, 'can').mockImplementation(() => allowed);
    vi.spyOn(session, 'whyNot').mockImplementation(() =>
      allowed ? null : 'Traders and admins only.',
    );
  });

  afterEach(() => controller.verify());

  async function render(list = [MAIN, LIVE]) {
    fixture = TestBed.createComponent(PortfoliosPanel);
    fixture.detectChanges();
    (await nextRequest(controller, '/api/portfolios')).flush(page(list));
    await tick();
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  /** One read after a change: the panel shows the context's list, like the picker (UX-69). */
  async function flushLists(list: unknown[]) {
    (await nextRequest(controller, '/api/portfolios')).flush(page(list));
    await tick();
    fixture.detectChanges();
  }

  function button(root: Element, text: string) {
    return [...root.querySelectorAll('button')].find((b) => b.textContent?.trim() === text)!;
  }

  function type(el: HTMLElement, selector: string, value: string) {
    const input = el.querySelector<HTMLInputElement>(selector)!;
    input.value = value;
    input.dispatchEvent(new Event('input'));
  }

  it('lists your portfolios with a paper or live stamp', async () => {
    const el = await render();
    const rows = [...el.querySelectorAll('.books li')];
    expect(rows[0].textContent).toContain('Main');
    expect(rows[0].textContent).toContain('PAPER');
    expect(rows[0].textContent).toContain('Default');
    expect(rows[1].textContent).toContain('LIVE');
  });

  it('links a live portfolio, and only a live one, to its live settings', async () => {
    const el = await render();
    const links = [...el.querySelectorAll<HTMLAnchorElement>('.books li a')];
    expect(links.map((a) => a.textContent?.trim())).toEqual(['Live settings']);
    expect(links[0].getAttribute('href')).toBe('/profile/live/pf_2');
  });

  it('opens a paper portfolio and reloads the picker', async () => {
    const el = await render();
    type(el, '#new-portfolio-name', '  Swing ');
    type(el, '#new-portfolio-cash', '25000');
    el.querySelector<HTMLFormElement>('form.new')!.dispatchEvent(new Event('submit'));
    const req = await nextRequest(controller, '/api/portfolios', 'POST');
    expect(req.request.body).toEqual({ name: 'Swing', initial_cash: 25000 });
    req.flush(book({ id: 'pf_3', name: 'Swing', initial_cash: 25000 }));
    await flushLists([MAIN, LIVE, book({ id: 'pf_3', name: 'Swing' })]);
    expect(el.querySelectorAll('.books li').length).toBe(3);
  });

  it('checks the name before sending', async () => {
    const el = await render();
    el.querySelector<HTMLFormElement>('form.new')!.dispatchEvent(new Event('submit'));
    fixture.detectChanges();
    expect(el.textContent).toContain('Enter a name.');
    controller.expectNone({ method: 'POST', url: '/api/portfolios' });
  });

  it('renames a portfolio', async () => {
    const el = await render();
    button(el, 'Rename').click();
    fixture.detectChanges();
    const sheet = el.querySelector('#rename-portfolio-form')!;
    expect(button(sheet, 'Rename').disabled).toBe(true);
    type(el, '#rename-portfolio', 'Core');
    fixture.detectChanges();
    button(sheet, 'Rename').click();
    const req = await nextRequest(controller, '/api/portfolios/pf_1', 'PATCH');
    expect(req.request.body).toEqual({ name: 'Core' });
    req.flush({ ...MAIN, name: 'Core' });
    await flushLists([{ ...MAIN, name: 'Core' }, LIVE]);
    expect(el.querySelector('#rename-portfolio-form')).toBeNull();
    expect(el.textContent).toContain('Core');
  });

  it('locks the actions without portfolio.manage', async () => {
    allowed = false;
    const el = await render();
    expect(button(el, 'Rename').disabled).toBe(true);
    expect(button(el, 'Open portfolio').disabled).toBe(true);
    expect(el.textContent).toContain('Traders and admins only.');
  });

  it('shows the same list as the portfolio picker (UX-69)', async () => {
    await render();
    const ctx = TestBed.inject(PortfolioContextService);
    expect(ctx.options().map((p) => p.id)).toEqual(['pf_1', 'pf_2']);
  });

  it("an invalid field's describedby resolves to the error (UX-61)", async () => {
    const el = await render();
    type(el, '#new-portfolio-cash', '-5');
    el.querySelector<HTMLFormElement>('form.new')!.dispatchEvent(new Event('submit'));
    fixture.detectChanges();
    const name = el.querySelector<HTMLInputElement>('#new-portfolio-name')!;
    expect(name.getAttribute('aria-invalid')).toBe('true');
    expect(el.querySelector('#' + name.getAttribute('aria-describedby'))?.textContent).toContain(
      'Enter a name.',
    );
    const cash = el.querySelector<HTMLInputElement>('#new-portfolio-cash')!;
    expect(el.querySelector('#' + cash.getAttribute('aria-describedby'))?.textContent).toContain(
      'above 0',
    );
  });
});
