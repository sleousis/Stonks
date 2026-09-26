import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  resource,
  signal,
  viewChild,
} from '@angular/core';

import { HaltsService, RESUME_CONFIRMATION } from '../../api/halts.service';
import type { HaltView, KillSwitchRequest } from '../../api/models';
import { StepUpService, isStepUpRequired } from '../../core/auth/step-up.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { HaltStateService, haltScopeText } from '../../core/halts/halt-state.service';
import { ToastService } from '../../core/notify/toast.service';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { PageHeader } from '../../shared/ui/page-header';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusChangeDialog } from '../../shared/ui/status-change-dialog';
import { StatusPill } from '../../shared/ui/status-pill';
import { HALT_KIND_LABEL, HALT_STOPS_LABEL, haltAction } from './halt-labels';

type KillScope = 'global' | 'portfolio';

/**
 * Halts stop new orders before they reach the broker. Shows the active
 * ones with their way out (resume a kill switch, clear the others), turns
 * the kill switch on, and lists past halts.
 */
@Component({
  selector: 'app-halts-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    PageHeader,
    DataTable,
    TableCell,
    StatusPill,
    LoadingState,
    EmptyState,
    ErrorState,
    StatusChangeDialog,
  ],
  templateUrl: './halts.page.html',
  styleUrl: './halts.page.scss',
})
export class HaltsPage {
  private readonly api = inject(HaltsService);
  private readonly state = inject(HaltStateService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);
  private readonly stepUp = inject(StepUpService);
  private readonly dialog = viewChild.required(StatusChangeDialog);

  /** Every halt, cleared ones included; split into active and past below. */
  protected readonly halts = resource({ loader: () => this.api.list(true) });
  protected readonly active = computed(() =>
    this.halts.hasValue() ? this.halts.value().filter((h) => h.active) : [],
  );
  protected readonly past = computed(() =>
    this.halts.hasValue() ? this.halts.value().filter((h) => !h.active) : [],
  );

  protected readonly busyId = signal<number | null>(null);

  // ---- kill switch form ---------------------------------------------------
  protected readonly scope = signal<KillScope>('global');
  protected readonly portfolioId = signal('pf_default');
  protected readonly reason = signal('');
  protected readonly flatten = signal(false);
  protected readonly submitted = signal(false);
  protected readonly engaging = signal(false);

  protected readonly reasonError = computed(() =>
    this.reason().trim() ? null : 'Say why, so others know when it is safe to resume.',
  );
  protected readonly portfolioError = computed(() =>
    this.scope() === 'portfolio' && !this.portfolioId().trim() ? 'Enter a portfolio id.' : null,
  );

  protected readonly kindLabel = HALT_KIND_LABEL;
  protected readonly stopsLabel = HALT_STOPS_LABEL;
  protected readonly action = haltAction;
  protected readonly scopeText = haltScopeText;
  protected readonly haltKey = (h: HaltView) => String(h.id);

  protected readonly activeColumns: TableColumn<HaltView>[] = [
    { key: 'kind', label: 'Kind', value: (h) => HALT_KIND_LABEL[h.kind], mobile: 'title' },
    { key: 'scope', label: 'Scope', value: haltScopeText },
    { key: 'halt', label: 'Stops', value: (h) => HALT_STOPS_LABEL[h.halt] },
    { key: 'reason', label: 'Reason' },
    { key: 'tripped_by', label: 'By' },
    { key: 'tripped_at', label: 'Since', format: 'datetime' },
    { key: 'action', label: 'Action', sortable: false, value: () => '' },
  ];

  protected readonly pastColumns: TableColumn<HaltView>[] = [
    { key: 'kind', label: 'Kind', value: (h) => HALT_KIND_LABEL[h.kind], mobile: 'title' },
    { key: 'scope', label: 'Scope', value: haltScopeText },
    { key: 'reason', label: 'Reason' },
    { key: 'tripped_by', label: 'Tripped by', mobile: 'hide' },
    { key: 'tripped_at', label: 'Tripped', format: 'datetime' },
    { key: 'cleared_by', label: 'Ended by', value: (h) => h.cleared_by ?? 'Expired' },
    { key: 'cleared_at', label: 'Ended', format: 'datetime' },
    { key: 'clear_reason', label: 'Why it ended', mobile: 'hide' },
  ];

  private async reload(): Promise<void> {
    this.halts.reload();
    await this.state.refresh();
  }

  async engage(): Promise<void> {
    this.submitted.set(true);
    if (this.reasonError() || this.portfolioError() || this.engaging()) return;
    const scope = this.scope();
    const target =
      scope === 'global' ? 'every portfolio' : `portfolio ${this.portfolioId().trim()}`;
    const ok = await this.confirm.confirm({
      title: 'Turn on the kill switch?',
      message: this.flatten()
        ? `New buys stop for ${target}. Sells and exits still go out so positions can close.`
        : `Every new order stops for ${target} until someone resumes trading.`,
      confirmLabel: 'Engage kill switch',
      tone: 'danger',
    });
    if (!ok) return;
    const body: KillSwitchRequest = {
      scope,
      reason: this.reason().trim(),
      flatten: this.flatten(),
      portfolio_id: scope === 'portfolio' ? this.portfolioId().trim() : null,
    };
    this.engaging.set(true);
    try {
      await this.api.kill(body);
      this.toasts.success(`Engaged the kill switch for ${target}.`);
      this.reason.set('');
      this.submitted.set(false);
      await this.reload();
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.engaging.set(false);
    }
  }

  async resume(h: HaltView): Promise<void> {
    const body = await this.dialog().open({
      title: 'Resume trading?',
      message: `Turns off the kill switch for ${haltScopeText(h).toLowerCase()}. Orders go out again from the next tick.`,
      confirmLabel: 'Resume trading',
      tone: 'danger',
      minReason: 1,
      typedConfirmation: RESUME_CONFIRMATION,
      reasonHint: 'Kept in the audit log.',
    });
    if (!body) return;
    if (!(await this.stepUp.ensure('Resume trading'))) return;
    this.busyId.set(h.id);
    try {
      const request = { confirmation: RESUME_CONFIRMATION, reason: body.reason ?? '' };
      try {
        await this.api.resume(h.id, request);
      } catch (e) {
        if (!isStepUpRequired(e)) throw e;
        if (!(await this.stepUp.ensure('Resume trading', { force: true }))) {
          this.toasts.error(
            'Resuming needs a second factor from the last few minutes. Sign in with your code, then try again.',
          );
          return;
        }
        await this.api.resume(h.id, request);
      }
      this.toasts.success('Resumed trading.');
      await this.reload();
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.busyId.set(null);
    }
  }

  async clear(h: HaltView): Promise<void> {
    const body = await this.dialog().open({
      title: `Clear the ${HALT_KIND_LABEL[h.kind].toLowerCase()} halt?`,
      message: 'New orders go out again from the next tick, unless another halt still applies.',
      confirmLabel: 'Clear halt',
      tone: 'danger',
      minReason: 1,
      reasonHint: 'Kept in the audit log and the status history.',
    });
    if (!body) return;
    this.busyId.set(h.id);
    try {
      await this.api.clear(h.id, { reason: body.reason ?? '' });
      this.toasts.success('Cleared the halt.');
      await this.reload();
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.busyId.set(null);
    }
  }
}
