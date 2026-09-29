import { Injectable } from '@angular/core';

import { SILENT_HEADERS } from '../core/http/interceptors';
import { allItems, unwrap } from './api-call';
import {
  commitStatementImport,
  listStatementImports,
  listStatementPresets,
  previewStatementImport,
  undoStatementImport,
} from './generated/sdk.gen';
import type { StatementImportRequest } from './models';

/** CSV statements for brokers without an API: preview, import, undo (roadmap 23.17). */
@Injectable({ providedIn: 'root' })
export class StatementImportsService {
  list() {
    return allItems((query) => unwrap(listStatementImports({ query })));
  }

  /** The broker exports read without a column mapping (DEGIRO first). */
  presets() {
    return unwrap(listStatementPresets());
  }

  /** What an import would do. Silent: the page shows a bad file or mapping inline. */
  preview(body: StatementImportRequest) {
    return unwrap(previewStatementImport({ body, headers: SILENT_HEADERS }));
  }

  /** Import the new rows. Silent: the page shows a refusal inline. */
  commit(body: StatementImportRequest) {
    return unwrap(commitStatementImport({ body, headers: SILENT_HEADERS }));
  }

  undo(importId: string) {
    return unwrap(undoStatementImport({ path: { import_id: importId } }));
  }
}
