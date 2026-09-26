import { Injectable } from '@angular/core';

import { unwrap } from './api-call';
import { getIngestResult, listDataSources, listIngestRuns, startIngest } from './generated/sdk.gen';
import type { IngestRequest, ListIngestRunsData } from './models';

/** Ingest runs, data sources, and triggering an ingest (a background job). */
@Injectable({ providedIn: 'root' })
export class IngestService {
  runs(query?: ListIngestRunsData['query']) {
    return unwrap(listIngestRuns({ query }));
  }

  start(body: IngestRequest) {
    return unwrap(startIngest({ body }));
  }

  result(jobId: string) {
    return unwrap(getIngestResult({ path: { job_id: jobId } }));
  }

  sources() {
    return unwrap(listDataSources());
  }
}
