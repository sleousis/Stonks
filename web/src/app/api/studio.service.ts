import { Injectable } from '@angular/core';

import { SILENT_HEADERS } from '../core/http/interceptors';
import { unwrap } from './api-call';
import {
  createDraft,
  deleteDraft,
  disableDraft,
  enableDraft,
  getDraft,
  getRuleSpecSchema,
  listDrafts,
  listStudioTemplates,
  registerDraft,
  startDraftBacktest,
  startDraftLabRun,
  updateDraft,
  validateDraft,
  validateRuleSpec,
} from './generated/sdk.gen';
import type {
  DraftBacktestRequest,
  DraftCreate,
  DraftLabRunRequest,
  DraftUpdate,
  ListDraftsData,
  SpecValidateRequest,
  ValidateRequest,
} from './models';

/** Strategy Studio: rule-spec drafts, validation, draft backtests/lab runs, register. */
@Injectable({ providedIn: 'root' })
export class StudioService {
  drafts(query?: ListDraftsData['query']) {
    return unwrap(listDrafts({ query }));
  }

  draft(draftId: string) {
    return unwrap(getDraft({ path: { draft_id: draftId } }));
  }

  create(body: DraftCreate) {
    return unwrap(createDraft({ body }));
  }

  update(draftId: string, body: DraftUpdate) {
    return unwrap(updateDraft({ path: { draft_id: draftId }, body }));
  }

  delete(draftId: string) {
    return unwrap(deleteDraft({ path: { draft_id: draftId } }));
  }

  validate(draftId: string, body?: ValidateRequest | null) {
    return unwrap(validateDraft({ path: { draft_id: draftId }, body }));
  }

  /** `silent` skips the error toast (the builder validates in the background as you type). */
  validateSpec(body: SpecValidateRequest, silent = false) {
    return unwrap(validateRuleSpec({ body, headers: silent ? SILENT_HEADERS : undefined }));
  }

  startBacktest(draftId: string, body: DraftBacktestRequest) {
    return unwrap(startDraftBacktest({ path: { draft_id: draftId }, body }));
  }

  startLabRun(draftId: string, body: DraftLabRunRequest) {
    return unwrap(startDraftLabRun({ path: { draft_id: draftId }, body }));
  }

  register(draftId: string) {
    return unwrap(registerDraft({ path: { draft_id: draftId } }));
  }

  enable(draftId: string) {
    return unwrap(enableDraft({ path: { draft_id: draftId } }));
  }

  disable(draftId: string) {
    return unwrap(disableDraft({ path: { draft_id: draftId } }));
  }

  schema() {
    return unwrap(getRuleSpecSchema());
  }

  templates() {
    return unwrap(listStudioTemplates());
  }
}
