import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { ModelVersionView } from '../../api/model-versions.service';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { JobsService } from '../../core/jobs/jobs.service';
import { ADMIN } from '../../../testing/auth-fixtures';
import { nextRequest, page, tick } from '../../../testing/http';
import {
  CANDIDATE_V2,
  MODEL_ID,
  RETRAIN_RESULT,
  finishedJob,
} from '../../../testing/model-version-fixtures';
import { ModelsPage } from './models.page';

describe('ModelsPage', () => {
  let fixture: ComponentFixture<ModelsPage>;
  let http: HttpTestingController;
  let el: HTMLElement;

  async function render(candidates: ModelVersionView[]) {
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        ...provideApi(),
        provideHttpClientTesting(),
        { provide: JobsService, useValue: { track: (id: string) => finishedJob(id) } },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    const loading = TestBed.inject(SessionService).load();
    (await nextRequest(http, '/api/auth/me')).flush(ADMIN);
    await loading;
    fixture = TestBed.createComponent(ModelsPage);
    el = fixture.nativeElement;
    fixture.detectChanges();
    (await nextRequest(http, '/api/model-versions/candidates')).flush(page(candidates));
    await tick();
    fixture.detectChanges();
  }

  afterEach(() => http.verify());

  it('lists each candidate with a link to its versions tab', async () => {
    await render([CANDIDATE_V2]);
    expect(el.querySelector('h1')!.textContent).toContain('Model versions');
    const link = el.querySelector<HTMLAnchorElement>('app-data-table a')!;
    expect(link.getAttribute('href')).toBe(`/strategies/${MODEL_ID}?tab=versions`);
    expect(link.getAttribute('aria-label')).toContain('Review v2');
  });

  it('says when nothing waits', async () => {
    await render([]);
    expect(el.textContent).toContain('No candidates');
  });

  it('retrains every strategy and reloads the candidates', async () => {
    await render([]);
    el.querySelector<HTMLButtonElement>('app-retrain-job button[type=submit]')!.click();
    const post = await nextRequest(http, '/api/model-versions/retrain', 'POST');
    expect(post.request.body).toEqual({ strategy_ids: null, force: false });
    post.flush({ id: 'job_r1', kind: 'model_retrain', params: {}, status: 'queued', progress: 0 });
    (await nextRequest(http, '/api/model-versions/jobs/job_r1/result')).flush(RETRAIN_RESULT);
    (await nextRequest(http, '/api/model-versions/candidates')).flush(page([CANDIDATE_V2]));
    await tick();
    fixture.detectChanges();
    expect(el.querySelectorAll('app-data-table tbody tr').length).toBe(1);
  });
});
