import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { Component } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { BrokerInfo, Job, TickResultView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { JOB_FETCH, JOB_POLL_MS } from '../../core/jobs/jobs.service';
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

  beforeEach(async () => {
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
    fixture = TestBed.createComponent(Host);
    el = fixture.nativeElement.querySelector('app-tick-runner');
    fixture.detectChanges();
    (await nextRequest(controller, '/api/brokers')).flush(PAPER);
    await settle();
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

  it('starts with dry run on and the broker shown', () => {
    expect(dryRunBox().checked).toBe(true);
    expect(runButton().textContent).toContain('Run dry run');
    expect(el.querySelector('.broker')?.textContent).toContain('alpaca paper');
  });

  it('runs a dry run after a plain confirmation, no typing', async () => {
    runButton().click();
    await settle();

    expect(dialogEl().textContent).toContain('Run a dry-run tick?');
    expect(dialogEl().querySelector('#confirm-typed')).toBeNull();
    const confirm = dialogButton('Run dry run')!;
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
    expect(el.querySelector('.result a')?.getAttribute('href')).toBe('/orders/ticks/20260926-abc');
  });

  it('makes a real tick show the broker and require typing its label', async () => {
    dryRunBox().click();
    await settle();
    expect(el.querySelector('.alert')?.textContent).toContain('alpaca paper');
    expect(runButton().textContent).toContain('Run tick');

    runButton().click();
    await settle();

    expect(dialogEl().textContent).toContain('Run a real tick on the alpaca paper broker?');
    const typed = dialogEl().querySelector<HTMLInputElement>('#confirm-typed')!;
    expect(typed).not.toBeNull();
    expect(dialogButton('Run tick')!.disabled).toBe(true);

    typed.value = 'alpaca';
    typed.dispatchEvent(new Event('input'));
    await settle();
    expect(dialogButton('Run tick')!.disabled).toBe(true);

    typed.value = 'alpaca paper';
    typed.dispatchEvent(new Event('input'));
    await settle();
    dialogButton('Run tick')!.click();

    const post = await nextRequest(controller, '/api/ticks', 'POST');
    expect(post.request.body).toMatchObject({ dry_run: false });
    post.flush(job('queued'));
    (await nextRequest(controller, '/api/jobs/job-1')).flush(job('succeeded', 1));
    (await nextRequest(controller, '/api/ticks/jobs/job-1/result')).flush(RESULT);
    await settle();

    expect(el.querySelector('.result')?.textContent).toContain('momentum-v3');
  });

  it('sends nothing when the real tick is cancelled', async () => {
    dryRunBox().click();
    await settle();
    runButton().click();
    await settle();
    dialogButton('Cancel')!.click();
    await settle();
    expect(controller.match((r) => r.method === 'POST').length).toBe(0);
  });
});
