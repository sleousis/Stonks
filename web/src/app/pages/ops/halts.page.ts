import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  linkedSignal,
  resource,
  signal,
  viewChild,
} from '@angular/core';

import { HaltsService, RESUME_CONFIRMATION } from '../../api/halts.service';
import type { HaltView, KillSwitchRequest } from '../../api/models';
import type { Permission } from '../../core/auth/permissions';
import { SessionService } from '../../core/auth/session.service';
import { StepUpService } from '../../core/auth/step-up.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { HaltStateService } from '../../core/halts/halt-state.service';
import { ToastService } from '../../core/notify/toast.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { PageHeader } from '../../shared/ui/page-header';
import { PermissionNote } from '../../shared/ui/permission-note';
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
    PermissionNote,
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
  protected readonly session = inject(SessionService);
  private readonly portfolios = inject(PortfolioContextService);

  /** Turning the kill switch on (one portfolio). */
  protected readonly canKill = computed(() => this.session.can('killswitch.user'));
  /** Every portfolio at once: admins only. */
  protected readonly canKillGlobal = computed(() => this.session.can('killswitch.global'));
  /** The caller's portfolios, when the server lists them (else a typed id). */
  protected readonly portfolioOptions = this.portfolios.options;

  /** Every halt, cleared ones included; split into active and past below. */
  protected readonly halts = resource({ loader: () => this.api.list(true) });
  protected readonly active = computed(() =>
    this.halts.hasValue() ? this.halts.value().filter((h) => h.active) : [],
  );
  protected readonly past = computed(() =>
    this.halts.hasValue() ? this.halts.value().filter((h) => !h.active) : [],
  );

  /** A kill switch is on: the page head turns red, like the session strip. */
  protected readonly killOn = computed(() => this.active().some((h) => h.kind === 'kill'));

  protected readonly busyId = signal<number | null>(null);

  // ---- kill switch form ---------------------------------------------------
  /** Admins start on every portfolio; everyone else can only stop one. */
  protected readonly scope = linkedSignal<KillScope>(() =>
    this.canKillGlobal() ? 'global' : 'portfolio',
  );
  protected readonly portfolioId = linkedSignal(
    () => this.portfolios.current()?.id ?? 'pf_default',
  );
  protected readonly reason = signal('');
  protected readonly buysOnly = signal(false);
  protected readonly submitted = signal(false);
  protected readonly engaging = signal(false);

  protected readonly reasonError = computed(() =>
    this.reason().trim() ? null : 'Say why, so others know when it is safe to resume.',
  );
  protected readonly portfolioError = computed(() =>
    this.scope() === 'portfolio' && !this.portfolioId().trim() ? 'Pick a portfolio.' : null,
  );

  protected readonly kindLabel = HALT_KIND_LABEL;
  protected readonly stopsLabel = HALT_STOPS_LABEL;
  protected readonly action = haltAction;
  /** Who the halt covers, with portfolio names instead of ids. */
  protected readonly scopeText = (h: HaltView): string => {
    if (h.scope === 'portfolio') {
      return h.portfolio_id ? `Portfolio ${this.portfolioName(h.portfolio_id)}` : 'One portfolio';
    }
    if (h.scope === 'user') return "One trader's portfolios";
    return 'Every portfolio';
  };
  protected readonly haltKey = (h: HaltView) => String(h.id);

  protected readonly activeColumns: TableColumn<HaltView>[] = [
    { key: 'kind', label: 'Kind', value: (h) => HALT_KIND_LABEL[h.kind], mobile: 'title' },
    { key: 'scope', label: 'Scope', value: this.scopeText },
    { key: 'halt', label: 'Stops', value: (h) => HALT_STOPS_LABEL[h.halt] },
    { key: 'reason', label: 'Reason' },
    { key: 'tripped_by', label: 'By' },
    { key: 'tripped_at', label: 'Since', format: 'datetime' },
    { key: 'action', label: 'Action', sortable: false, value: () => '' },
  ];

  protected readonly pastColumns: TableColumn<HaltView>[] = [
    { key: 'kind', label: 'Kind', value: (h) => HALT_KIND_LABEL[h.kind], mobile: 'title' },
    { key: 'scope', label: 'Scope', value: this.scopeText },
    { key: 'reason', label: 'Reason' },
    { key: 'tripped_by', label: 'Tripped by', mobile: 'hide' },
    { key: 'tripped_at', label: 'Tripped', format: 'datetime' },
    { key: 'cleared_by', label: 'Ended by', value: (h) => h.cleared_by ?? 'Expired' },
    { key: 'cleared_at', label: 'Ended', format: 'datetime' },
    { key: 'clear_reason', label: 'Why it ended', mobile: 'hide' },
  ];

  constructor() {
    void this.portfolios.load();
  }

  /** What ending this halt needs: global halts are the admins'. */
  protected endPermission(h: HaltView): Permission {
    if (h.scope === 'global') return 'killswitch.global';
    return haltAction(h) === 'resume' ? 'killswitch.resume' : 'risk.reset';
  }

  /** "Portfolio Main book" rather than the portfolio's id, when we know its name. */
  protected portfolioName(id: string): string {
    return this.portfolioOptions().find((p) => p.id === id)?.name ?? id;
  }

  private async reload(): Promise<void> {
    this.halts.reload();
    await this.state.refresh();
  }

  async engage(): Promise<void> {
    this.submitted.set(true);
    if (!this.canKill() || this.reasonError() || this.portfolioError() || this.engaging()) return;
    const scope = this.scope();
    const target =
      scope === 'global'
        ? 'every portfolio'
        : `portfolio ${this.portfolioName(this.portfolioId().trim())}`;
    const ok = await this.confirm.confirm({
      title: 'Turn on the kill switch?',
      message: this.buysOnly()
        ? `New buys stop for ${target}. Sells and exits still go out. No position is closed.`
        : `Every new order stops for ${target} until someone resumes trading.`,
      confirmLabel: 'Engage kill switch',
      tone: 'danger',
    });
    if (!ok) return;
    const body: KillSwitchRequest = {
      scope,
      reason: this.reason().trim(),
      buys_only: this.buysOnly(),
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
      message: `Turns off the kill switch (${this.scopeText(h)}). Orders go out again from the next trading run.`,
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
      // A stale second factor comes back as 403 step_up_required: the session
      // interceptor prompts for a code and retries once.
      await this.api.resume(h.id, { confirmation: RESUME_CONFIRMATION, reason: body.reason ?? '' });
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
      message:
        'New orders go out again from the next trading run, unless another halt still applies.',
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
