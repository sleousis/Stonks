import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { Component } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { BrokerInfo, Job, TickResultView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { TicksService } from '../../api/ticks.service';
import { SessionService } from '../../core/auth/session.service';
import { JOB_FETCH, JOB_POLL_MS } from '../../core/jobs/jobs.service';
import { ADMIN, TRADER } from '../../../testing/auth-fixtures';
import { ConfirmDialog } from '../../shared/ui/confirm-dialog';
import { nextRequest, tick } from '../../../testing/http';
import { TickRunner } from './tick-runner';

const PAPER: BrokerInfo = {
  kind: 'alpaca',
  paper: true,
  allow_live: false,
  credentials_configured: true,
};

function job(status: Job['status'], progress = 0): Job {
  return {
    id: 'job-1',
    kind: 'tick',
    status,
    progress,
    params: {},
    created_at: '2026-09-26T08:00:00Z',
  };
}

const RESULT: TickResultView = {
  tick_id: '20260926-abc',
  status: 'ok',
  dry_run: false,
  orders_placed: 2,
  fills: 2,
  winner_strategy_id: 'momentum-v3',
};

/** The runner plus the shell's confirm dialog, attached to the same document. */
@Component({
  imports: [TickRunner, ConfirmDialog],
  template: `<app-tick-runner /><app-confirm-dialog />`,
})
class Host {}

describe('TickRunner', () => {
  let fixture: ComponentFixture<Host>;
  let controller: HttpTestingController;
  let el: HTMLElement;

  async function setup(me = ADMIN): Promise<void> {
    TestBed.configureTestingModule({
      imports: [Host],
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        provideRouter([]),
        // No event stream in tests: the service falls back to polling.
        { provide: JOB_FETCH, useValue: () => Promise.reject(new Error('no stream')) },
        { provide: JOB_POLL_MS, useValue: 1 },
      ],
    });
    controller = TestBed.inject(HttpTestingController);
    const loading = TestBed.inject(SessionService).load();
    (await nextRequest(controller, '/api/auth/me')).flush(me);
    await loading;
    fixture = TestBed.createComponent(Host);
    el = fixture.nativeElement.querySelector('app-tick-runner');
    fixture.detectChanges();
    (await nextRequest(controller, '/api/brokers')).flush(PAPER);
    await settle();
  }

  beforeEach(async () => {
    if (!expect.getState().currentTestName?.includes('trader')) await setup();
  });

  afterEach(() => controller.verify());

  async function settle(): Promise<void> {
    for (let i = 0; i < 4; i++) {
      await tick(5);
      fixture.detectChanges();
    }
  }

  const dialogEl = () =>
    (fixture.nativeElement as HTMLElement).querySelector('app-confirm-dialog') as HTMLElement;
  const dialogButton = (label: string) =>
    [...dialogEl().querySelectorAll<HTMLButtonElement>('button')].find(
      (b) => b.textContent?.trim() === label,
    );
  const runButton = () => el.querySelector<HTMLButtonElement>('button[type="submit"]')!;
  const dryRunBox = () => el.querySelector<HTMLInputElement>('input[name="dryRun"]')!;
  const ticketEl = dialogEl;
  const ticketButton = dialogButton;

  it('starts with dry run on and the broker shown with its PAPER stamp', () => {
    expect(dryRunBox().checked).toBe(true);
    expect(runButton().textContent).toContain('Start dry run');
    expect(el.querySelector('.broker')?.textContent).toContain('alpaca paper');
    expect(el.querySelector('.broker app-mode-stamp')?.textContent).toContain('PAPER');
    expect(el.querySelector('.permission-note')).toBeNull();
  });

  it('a trader sees why the run button is off', async () => {
    await setup(TRADER);
    expect(runButton().disabled).toBe(true);
    expect(el.querySelector('.permission-note')?.textContent).toContain('Admins only.');
    runButton().click();
    await settle();
    expect(controller.match((r) => r.method === 'POST').length).toBe(0);
  });

  it('runs a dry run after a plain confirmation, no typing', async () => {
    runButton().click();
    await settle();

    expect(dialogEl().textContent).toContain('Start a dry run?');
    expect(dialogEl().querySelector('#confirm-typed')).toBeNull();
    const confirm = dialogButton('Start dry run')!;
    expect(confirm.disabled).toBe(false);
    confirm.click();
    await settle();

    const post = await nextRequest(controller, '/api/ticks', 'POST');
    expect(post.request.body).toEqual({ dry_run: true, as_of: null, tickers: null });
    post.flush(job('queued'));

    (await nextRequest(controller, '/api/jobs/job-1')).flush(job('succeeded', 1));
    (await nextRequest(controller, '/api/ticks/jobs/job-1/result')).flush({
      ...RESULT,
      dry_run: true,
      fills: 0,
    });
    await settle();

    expect(el.querySelector('.result')?.textContent).toContain('Dry-run result');
    const link = el.querySelector('.result a');
    expect(link?.getAttribute('href')).toBe('/orders/ticks/20260926-abc');
    expect(link?.textContent?.trim()).toBe('Open this run');
  });

  it('result 500 after success shows an inline error with retry', async () => {
    const ticks = TestBed.inject(TicksService);
    runButton().click();
    await settle();
    dialogButton('Start dry run')!.click();
    await settle();
    (await nextRequest(controller, '/api/ticks', 'POST')).flush(job('queued'));
    (await nextRequest(controller, '/api/jobs/job-1')).flush(job('succeeded', 1));
    (await nextRequest(controller, '/api/ticks/jobs/job-1/result')).flush(
      { title: 'x', status: 500, detail: 'Result store unavailable.' },
      { status: 500, statusText: 'Server Error' },
    );
    await settle();

    expect(ticks.finished()).toBe(1);
    const error = el.querySelector('app-error-state');
    expect(error?.textContent).toContain('its result could not load');
    const retry = [...error!.querySelectorAll<HTMLButtonElement>('button')].find((b) =>
      b.textContent?.includes('Try again'),
    )!;
    retry.click();
    (await nextRequest(controller, '/api/ticks/jobs/job-1/result')).flush(RESULT);
    await settle();
    expect(el.querySelector('app-error-state')).toBeNull();
    expect(el.querySelector('.result')?.textContent).toContain('momentum-v3');
  });

  it('makes a real run show an order ticket and require typing the broker label', async () => {
    dryRunBox().click();
    await settle();
    expect(el.querySelector('.alert')?.textContent).toContain('alpaca paper');
    expect(runButton().textContent).toContain('Start trading run');

    runButton().click();
    await settle();

    expect(ticketEl().textContent).toContain('Trading run ticket');
    expect(ticketEl().querySelector('app-mode-stamp')?.textContent).toContain('PAPER');
    expect(ticketEl().textContent).toContain('All in the universe');
    const typed = ticketEl().querySelector<HTMLInputElement>('#confirm-typed')!;
    expect(typed).not.toBeNull();
    expect(ticketButton('Start trading run')!.disabled).toBe(true);

    typed.value = 'alpaca';
    typed.dispatchEvent(new Event('input'));
    await settle();
    expect(ticketButton('Start trading run')!.disabled).toBe(true);

    typed.value = 'alpaca paper';
    typed.dispatchEvent(new Event('input'));
    await settle();
    ticketButton('Start trading run')!.click();

    const post = await nextRequest(controller, '/api/ticks', 'POST');
    expect(post.request.body).toMatchObject({ dry_run: false });
    post.flush(job('queued'));
    (await nextRequest(controller, '/api/jobs/job-1')).flush(job('succeeded', 1));
    (await nextRequest(controller, '/api/ticks/jobs/job-1/result')).flush(RESULT);
    await settle();

    expect(el.querySelector('.result')?.textContent).toContain('momentum-v3');
  });

  it('sends nothing when the real run is cancelled', async () => {
    dryRunBox().click();
    await settle();
    runButton().click();
    await settle();
    ticketButton('Keep editing')!.click();
    await settle();
    expect(controller.match((r) => r.method === 'POST').length).toBe(0);
  });

  it('shows the ticket in the shared confirm sheet', async () => {
    dryRunBox().click();
    await settle();
    runButton().click();
    await settle();
    expect(el.querySelector('app-tick-ticket-dialog')).toBeNull();
    expect(ticketEl().querySelector('.ticket app-mode-stamp')?.textContent).toContain('PAPER');
    ticketButton('Keep editing')!.click();
    await settle();
  });

  it('dry run off plus a past date disables Start', async () => {
    const date = el.querySelector<HTMLInputElement>('#tick-as-of')!;
    date.value = '2020-01-02';
    date.dispatchEvent(new Event('change'));
    await settle();
    expect(runButton().disabled).toBe(false);

    dryRunBox().click();
    await settle();
    expect(runButton().disabled).toBe(true);
    expect(el.textContent).toContain('A real run trades today');

    date.value = '';
    date.dispatchEvent(new Event('change'));
    await settle();
    expect(runButton().disabled).toBe(false);
  });

  it('follows the run with the shared job progress', async () => {
    runButton().click();
    await settle();
    dialogButton('Start dry run')!.click();
    await settle();
    (await nextRequest(controller, '/api/ticks', 'POST')).flush(job('queued'));
    await settle();
    expect(el.querySelector('app-job-progress')?.textContent).toContain('Trading run:');
    (await nextRequest(controller, '/api/jobs/job-1')).flush({
      ...job('failed', 1),
      error: 'broker down',
    });
    await settle();
    expect(el.querySelectorAll('app-job-progress').length).toBe(1);
    expect(el.textContent!.match(/broker down/g)?.length).toBe(1);
  });
});
