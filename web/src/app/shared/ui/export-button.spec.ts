import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { csvName } from '../../api/exports.service';
import { provideApi } from '../../api/provide-api';
import { ToastService } from '../../core/notify/toast.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { nextRequest, tick } from '../../../testing/http';
import { ExportButton, saveFile } from './export-button';

describe('ExportButton', () => {
  let http: HttpTestingController;
  let clicked: string[];

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [...provideApi(), provideHttpClientTesting()] });
    http = TestBed.inject(HttpTestingController);
    clicked = [];
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clicked.push(this.download);
    });
    URL.createObjectURL = vi.fn(() => 'blob:csv');
    URL.revokeObjectURL = vi.fn();
  });

  afterEach(() => {
    http.verify();
    vi.restoreAllMocks();
  });

  function render(kind: 'orders' | 'lab-trials', runId: string | null = null) {
    const fixture = TestBed.createComponent(ExportButton);
    fixture.componentRef.setInput('kind', kind);
    fixture.componentRef.setInput('runId', runId);
    fixture.detectChanges();
    return fixture;
  }

  it('downloads the CSV over the API and saves it as a file', async () => {
    vi.spyOn(TestBed.inject(PortfolioContextService), 'query').mockReturnValue({
      portfolio_id: 'pf_1',
    });
    const fixture = render('orders');
    const button: HTMLButtonElement = fixture.nativeElement.querySelector('button');
    expect(button.textContent).toContain('Download CSV');
    button.click();
    const req = await nextRequest(http, '/api/exports/orders');
    expect(req.request.responseType).toBe('blob');
    expect(req.request.urlWithParams).toContain('portfolio_id=pf_1');
    req.flush(new Blob(['client_id\r\n'], { type: 'text/csv' }));
    await tick(5);
    expect(clicked).toEqual([csvName('orders', 'pf_1')]);
  });

  it('asks for one lab run by id', async () => {
    const fixture = render('lab-trials', 'lab_1');
    fixture.nativeElement.querySelector('button').click();
    const req = await nextRequest(http, '/api/exports/lab-trials');
    expect(req.request.urlWithParams).toContain('run_id=lab_1');
    req.flush(new Blob(['run_id\r\n']));
    await tick(5);
    expect(clicked[0]).toMatch(/^stonks-lab-trials-lab_1-\d{4}-\d{2}-\d{2}\.csv$/);
  });

  it("toasts the API's reason when the file cannot be made", async () => {
    const error = vi.spyOn(TestBed.inject(ToastService), 'error');
    const fixture = render('orders');
    fixture.nativeElement.querySelector('button').click();
    const body = new Blob(
      [JSON.stringify({ status: 404, title: 'Not Found', detail: 'you have no portfolio yet' })],
      {
        type: 'application/problem+json',
      },
    );
    (await nextRequest(http, '/api/exports/orders')).flush(body, {
      status: 404,
      statusText: 'Not Found',
    });
    await tick(5);
    expect(error).toHaveBeenCalledWith('you have no portfolio yet', 'Download failed');
    expect(clicked).toEqual([]);
  });

  it('names files like the server does', () => {
    expect(csvName('pnl', 'pf "x"', new Date('2026-09-27T10:00:00Z'))).toBe(
      'stonks-pnl-pf--x--2026-09-27.csv',
    );
    const doc = document;
    saveFile(doc, { blob: new Blob(['a']), filename: 'x.csv' });
    expect(clicked).toEqual(['x.csv']);
  });
});
