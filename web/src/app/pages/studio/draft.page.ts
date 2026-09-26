import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  effect,
  inject,
  input,
  linkedSignal,
  resource,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type { Draft, DraftValidation } from '../../api/models';
import { StudioService } from '../../api/studio.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { ApiError } from '../../core/http/api-error';
import { ToastService } from '../../core/notify/toast.service';
import { AgoPipe } from '../../shared/format.pipes';
import { PageHeader } from '../../shared/ui/page-header';
import { ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import { DraftShip } from './draft-ship';
import { DraftTest } from './draft-test';
import { RuleBuilder } from './rule-builder';
import {
  type RuleSpecDoc,
  type SpecIssue,
  assetClassesFromSchema,
  describePath,
  indicatorKindsFromSchema,
  localIssues,
  mergeIssues,
  toDoc,
  toIssueMap,
} from './rule-spec';
import { SpecJson } from './spec-json';

export type DraftTab = 'build' | 'test' | 'ship';
const VALIDATE_DEBOUNCE_MS = 400;

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const t = setTimeout(resolve, ms);
    signal.addEventListener('abort', () => {
      clearTimeout(t);
      reject(signal.reason);
    });
  });
}

/**
 * One draft: build the rules (visual builder + JSON), test them (backtest,
 * lab run) and ship them (register, enable / disable). Code drafts get a
 * plain source editor instead of the builder, when the API allows them.
 */
@Component({
  selector: 'app-draft-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    PageHeader,
    StatusPill,
    LoadingState,
    ErrorState,
    RuleBuilder,
    SpecJson,
    DraftTest,
    DraftShip,
    AgoPipe,
  ],
  templateUrl: './draft.page.html',
  styleUrl: './draft.page.scss',
  host: { '(window:beforeunload)': 'onBeforeUnload($event)' },
})
export class DraftPage {
  private readonly studio = inject(StudioService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);

  readonly id = input.required<string>();

  protected readonly draftRes = resource({
    params: () => ({ id: this.id() }),
    loader: ({ params }) => this.studio.draft(params.id),
  });
  protected readonly schema = resource({ loader: () => this.studio.schema() });
  protected readonly kinds = computed(() =>
    indicatorKindsFromSchema(this.schema.hasValue() ? this.schema.value() : null),
  );
  protected readonly assetClasses = computed(() =>
    assetClassesFromSchema(this.schema.hasValue() ? this.schema.value() : null),
  );

  /** The draft as last returned by the API (load, save, register, enable…). */
  protected readonly draft = linkedSignal<Draft | null>(() =>
    this.draftRes.hasValue() ? this.draftRes.value() : null,
  );
  protected readonly isCode = computed(() => this.draft()?.kind === 'code');

  // ---- rule drafts -----------------------------------------------------------
  /** Working copy the builder edits; reset only when the draft (re)loads. */
  protected readonly spec = linkedSignal<RuleSpecDoc | null>(() =>
    this.draftRes.hasValue() ? toDoc(this.draftRes.value().spec) : null,
  );
  private readonly savedJson = linkedSignal(() =>
    this.draftRes.hasValue() ? JSON.stringify(toDoc(this.draftRes.value().spec)) : '',
  );

  // ---- code drafts -----------------------------------------------------------
  protected readonly source = linkedSignal(() =>
    this.draftRes.hasValue() ? (this.draftRes.value().source_code ?? '') : '',
  );
  private readonly savedSource = linkedSignal(() =>
    this.draftRes.hasValue() ? (this.draftRes.value().source_code ?? '') : '',
  );
  protected readonly paramsText = linkedSignal(() =>
    this.draftRes.hasValue() ? JSON.stringify(this.draftRes.value().spec, null, 2) : '{}',
  );
  private readonly savedParams = linkedSignal(() =>
    this.draftRes.hasValue() ? JSON.stringify(this.draftRes.value().spec, null, 2) : '{}',
  );
  protected readonly paramsError = computed(() => {
    try {
      const v: unknown = JSON.parse(this.paramsText());
      return typeof v === 'object' && v !== null && !Array.isArray(v)
        ? null
        : 'Parameters must be a JSON object.';
    } catch {
      return 'Parameters are not valid JSON.';
    }
  });

  protected readonly dirty = computed(() => {
    const d = this.draft();
    if (!d) return false;
    if (d.kind === 'code') {
      return this.source() !== this.savedSource() || this.paramsText() !== this.savedParams();
    }
    const s = this.spec();
    return !!s && JSON.stringify(s) !== this.savedJson();
  });

  protected readonly saving = signal(false);
  protected readonly tab = signal<DraftTab>('build');
  protected readonly renaming = signal(false);
  protected readonly nameDraft = signal('');

  // ---- validation --------------------------------------------------------------
  /** Validates the working spec against the API as you type (debounced, silent). */
  protected readonly validation = resource({
    params: () => (this.isCode() ? undefined : (this.spec() ?? undefined)),
    loader: async ({ params, abortSignal }) => {
      await sleep(VALIDATE_DEBOUNCE_MS, abortSignal);
      return this.studio.validateSpec({ spec: params as Record<string, unknown> }, true);
    },
  });
  protected readonly apiIssues = computed<SpecIssue[]>(() =>
    this.validation.hasValue() ? this.validation.value().issues : [],
  );
  protected readonly issues = computed<SpecIssue[]>(() => {
    const s = this.spec();
    if (!s) return [];
    // The API is the validator of record: where it has spoken about a field,
    // its message replaces the local one.
    const api = this.apiIssues();
    const covered = new Set(api.map((i) => i.path));
    const local = localIssues(s, this.kinds()).filter((i) => !covered.has(i.path));
    return mergeIssues(api, local);
  });
  protected readonly issueMap = computed(() => toIssueMap(this.issues()));
  protected readonly validationState = computed<'checking' | 'valid' | 'invalid' | 'unknown'>(
    () => {
      if (this.issues().length) return 'invalid';
      if (this.validation.isLoading()) return 'checking';
      if (this.validation.hasValue()) return this.validation.value().valid ? 'valid' : 'invalid';
      return 'unknown';
    },
  );
  protected readonly validationHint = computed(() => {
    const err = this.validation.error();
    if (!err) return null;
    return err instanceof ApiError && err.isAuth
      ? 'Enter the API token in Settings to check the rules against the API.'
      : 'Could not reach the API to check the rules; local checks still apply.';
  });

  protected readonly smoke = signal<DraftValidation | null>(null);
  protected readonly checking = signal(false);

  protected readonly describePath = describePath;

  /** Handed to the test and ship panels so they run on the saved draft. */
  protected readonly ensureSaved = (): Promise<boolean> => this.save();

  constructor() {
    // A fresh draft id starts on the build tab with no stale smoke result.
    effect(() => {
      this.id();
      this.smoke.set(null);
    });
  }

  protected setSpec(doc: RuleSpecDoc): void {
    this.spec.set(doc);
  }

  protected applyJson(raw: Record<string, unknown>): void {
    this.spec.set(toDoc(raw));
    this.toasts.info('Loaded the JSON into the builder.');
  }

  protected onDraftChanged(next: Draft): void {
    this.draft.set(next);
  }

  // ---- tabs ------------------------------------------------------------------
  protected readonly tabs: { id: DraftTab; label: string }[] = [
    { id: 'build', label: 'Build' },
    { id: 'test', label: 'Test' },
    { id: 'ship', label: 'Ship' },
  ];

  protected onTabKey(event: KeyboardEvent): void {
    const order = this.tabs.map((t) => t.id);
    const i = order.indexOf(this.tab());
    const next =
      event.key === 'ArrowRight'
        ? order[(i + 1) % order.length]
        : event.key === 'ArrowLeft'
          ? order[(i + order.length - 1) % order.length]
          : event.key === 'Home'
            ? order[0]
            : event.key === 'End'
              ? order[order.length - 1]
              : null;
    if (!next) return;
    event.preventDefault();
    this.tab.set(next);
    this.host.nativeElement.querySelector<HTMLElement>(`#tab-${next}`)?.focus();
  }

  // ---- saving ----------------------------------------------------------------
  /** Saves pending edits. Resolves true when nothing is left unsaved. */
  async save(): Promise<boolean> {
    const d = this.draft();
    if (!d) return false;
    if (!this.dirty()) return true;
    if (d.kind === 'code' && this.paramsError()) {
      this.toasts.error(this.paramsError() ?? '', 'Cannot save');
      return false;
    }
    this.saving.set(true);
    try {
      const body =
        d.kind === 'code'
          ? {
              source_code: this.source(),
              spec: JSON.parse(this.paramsText()) as Record<string, unknown>,
            }
          : { spec: this.spec() as Record<string, unknown> };
      const next = await this.studio.update(d.id, body);
      this.draft.set(next);
      if (d.kind === 'code') {
        this.savedSource.set(next.source_code ?? '');
        this.savedParams.set(this.paramsText());
      } else {
        this.savedJson.set(JSON.stringify(this.spec()));
      }
      this.toasts.success(`Saved ${next.name}.`);
      return true;
    } catch {
      // The error interceptor already showed the API's message.
      return false;
    } finally {
      this.saving.set(false);
    }
  }

  async discard(): Promise<void> {
    const ok = await this.confirm.confirm({
      title: 'Discard unsaved changes?',
      message: 'The draft goes back to its last saved version.',
      confirmLabel: 'Discard',
      tone: 'danger',
    });
    if (!ok) return;
    this.draftRes.reload();
  }

  /** Save, then smoke-run the strategy on sample data (or the given tickers). */
  async check(): Promise<void> {
    const d = this.draft();
    if (!d || this.checking()) return;
    this.checking.set(true);
    try {
      if (!(await this.save())) return;
      this.smoke.set(await this.studio.validate(d.id, {}));
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.checking.set(false);
    }
  }

  // ---- rename ----------------------------------------------------------------
  protected startRename(): void {
    this.nameDraft.set(this.draft()?.name ?? '');
    this.renaming.set(true);
  }

  async rename(): Promise<void> {
    const d = this.draft();
    const name = this.nameDraft().trim();
    if (!d || !name) return;
    if (name === d.name) {
      this.renaming.set(false);
      return;
    }
    try {
      const next = await this.studio.update(d.id, { name });
      this.draft.set(next);
      this.renaming.set(false);
      this.toasts.success(`Renamed to ${next.name}.`);
    } catch {
      // The error interceptor already showed the API's message.
    }
  }

  // ---- issues summary --------------------------------------------------------
  /** Focus the control for an issue path, or the closest enclosing one. */
  protected focusIssue(path: string): void {
    const root = this.host.nativeElement;
    let p = path;
    for (;;) {
      const el = root.querySelector<HTMLElement>(`[data-path="${CSS.escape(p)}"]`);
      if (el) {
        const target =
          el.matches('input, select, textarea, button') || el.tabIndex >= 0
            ? el
            : (el.querySelector<HTMLElement>('input, select, textarea, button') ?? el);
        target.scrollIntoView?.({ block: 'center', behavior: 'smooth' });
        target.focus({ preventScroll: true });
        return;
      }
      const cut = Math.max(p.lastIndexOf('.'), p.lastIndexOf('['));
      if (cut <= 0) return;
      p = p.slice(0, cut);
    }
  }

  // ---- leaving ---------------------------------------------------------------
  /** Route guard: ask before dropping unsaved edits. */
  async canLeave(): Promise<boolean> {
    if (!this.dirty()) return true;
    return this.confirm.confirm({
      title: 'Leave without saving?',
      message: 'Your changes to this draft have not been saved and will be lost.',
      confirmLabel: 'Leave',
      cancelLabel: 'Stay',
      tone: 'danger',
    });
  }

  protected onBeforeUnload(event: BeforeUnloadEvent): void {
    if (this.dirty()) event.preventDefault();
  }
}
