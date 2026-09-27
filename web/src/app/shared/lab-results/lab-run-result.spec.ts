import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { LAB_RUN_VIEW } from '../../../testing/lab-fixtures';
import { LabRunResultView } from './lab-run-result';

describe('LabRunResultView', () => {
  function render(ensureJobId: string | null): HTMLElement {
    TestBed.configureTestingModule({ providers: [provideRouter([])] });
    const fixture = TestBed.createComponent(LabRunResultView);
    fixture.componentRef.setInput('result', { ...LAB_RUN_VIEW, ensure_job_id: ensureJobId });
    fixture.detectChanges();
    return fixture.nativeElement;
  }

  it('says when missing data was fetched first', () => {
    expect(render('job_e').querySelector('.data-job')?.textContent).toContain(
      'Missing prices were fetched first',
    );
  });

  it('says nothing about a data job when there was none, and links the trial ledger', () => {
    const el = render(null);
    expect(el.querySelector('.data-job')).toBeNull();
    expect(el.querySelector('a[href="/lab/ledger"]')?.textContent).toContain('trial ledger');
  });
});
