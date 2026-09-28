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
import type { Permission } from '../../core/auth/permissions';
import { SessionService } from '../../core/auth/session.service';
import { StepUpService } from '../../core/auth/step-up.service';
import { formatDateTime } from '../../core/format/format';
import { HaltStateService } from '../../core/halts/halt-state.service';
import { StopTradingService } from '../../core/halts/stop-trading.service';
import { ToastService } from '../../core/notify/toast.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { UpdatedAgo, autoRefresh } from '../../shared/auto-refresh';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { HelpTip } from '../../shared/ui/help-tip';
import { PageHeader } from '../../shared/ui/page-header';
import { PermissionNote } from '../../shared/ui/permission-note';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusChangeDialog } from '../../shared/ui/status-change-dialog';
import { StatusPill } from '../../shared/ui/status-pill';
import { HALT_KIND_LABEL, HALT_STOPS_LABEL, haltAction } from './halt-labels';
import { ResumeSheet } from './resume-sheet';

type KillScope = KillSwitchRequest['scope'];

/**
 * Halts stop new orders before they reach the broker. Shows the active
 * ones with their way out (resume a kill switch, clear the others), opens
 * the one Stop trading sheet (M7: the same sheet as the session strip, on
 * the portfolio on screen, with a ticket and a reason), and lists past
 * halts. Resuming confirms as a ticket with the PAPER or LIVE stamp
 * (UX-51). The page refreshes itself and follows the app-wide halt state
 * (UX-45).
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
    HelpTip,
    UpdatedAgo,
    ResumeSheet,
  ],
  templateUrl: './halts.page.html',
  styleUrl: './halts.page.scss',
})
export class HaltsPage {
  private readonly api = inject(HaltsService);
  private readonly state = inject(HaltStateService);
  private readonly stopTrading = inject(StopTradingService);
  private readonly toasts = inject(ToastService);
  private readonly stepUp = inject(StepUpService);
  private readonly dialog = viewChild.required(StatusChangeDialog);
  private readonly resumeSheet = viewChild.required(ResumeSheet);
  protected readonly session = inject(SessionService);
  private readonly portfolios = inject(PortfolioContextService);

  /** Turning the kill switch on (one portfolio). */
  protected readonly canKill = computed(() => this.session.can('killswitch.user'));
  /** The caller's portfolios. */
  protected readonly portfolioOptions = this.portfolios.options;
  /** Real money somewhere in the caller's portfolios: the kill button wears the brass ring. */
  protected readonly anyLive = computed(() =>
    this.portfolioOptions().some((p) => p.trading === 'live'),
  );

  /** Every halt, cleared ones included; split into active and past below. */
  protected readonly halts = resource({ loader: () => this.api.list(true) });
  protected readonly active = computed(() =>
    this.halts.hasValue() ? this.halts.value().filter((h) => h.active) : [],
  );
  protected readonly past = computed(() =>
    this.halts.hasValue() ? this.halts.value().filter((h) => !h.active) : [],
  );

  /** Reload when the app-wide halt state changes (a halt tripped or ended elsewhere). */
  private readonly activeKey = computed(() =>
    this.state
      .active()
      .map((h) => `${h.id}:${h.halt}`)
      .join(','),
  );
  protected readonly auto = autoRefresh(() => [this.halts], { triggers: [this.activeKey] });

  /** A kill switch is on: the page head turns red, like the session strip. */
  protected readonly killOn = computed(() => this.active().some((h) => h.kind === 'kill'));

  protected readonly busyId = signal<number | null>(null);

  /** Where the Stop trading sheet starts: the portfolio on screen. */
  protected readonly onScreen = computed(
    () => this.portfolios.current()?.name ?? 'all your portfolios',
  );

  protected readonly kindLabel = HALT_KIND_LABEL;
  protected readonly stopsLabel = HALT_STOPS_LABEL;
  protected readonly action = haltAction;
  /** Who the halt covers, with portfolio names instead of ids (UX-17). */
  protected readonly scopeText = (h: Pick<HaltView, 'scope' | 'portfolio_id' | 'user_id'>) =>
    this.state.scopeText()(h);
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

  /** Real money under this scope: the halt's portfolio, or any of the caller's. */
  private liveFor(scope: KillScope, portfolioId: string | null): boolean {
    if (scope === 'portfolio') {
      return this.portfolioOptions().some((p) => p.id === portfolioId && p.trading === 'live');
    }
    return this.anyLive();
  }

  private async reload(): Promise<void> {
    this.halts.reload();
    await this.state.refresh();
  }

  /** Open the Stop trading sheet, the same one the session strip opens. */
  protected stop(): void {
    this.stopTrading.open.set(true);
  }

  /** Numbers each opening of the resume sheet. */
  private resumeOpening = 0;

  async resume(h: HaltView): Promise<void> {
    const sheet = this.resumeSheet();
    const opened = sheet.open({
      live: this.liveFor(h.scope, h.portfolio_id),
      lines: [
        { label: 'Scope', value: this.scopeText(h) },
        { label: 'Starts again', value: HALT_STOPS_LABEL[h.halt] },
        { label: 'Stopped since', value: formatDateTime(h.tripped_at) },
        { label: 'Why it stopped', value: h.reason || 'None given' },
      ],
    });
    // Roadmap 23.15: the resume checks show above the typed words. Only the
    // latest opening's answer may land: a slow one from an earlier opening
    // would show another check result under this ticket.
    const opening = ++this.resumeOpening;
    this.api.resumeChecks(h.id).then(
      (checks) => opening === this.resumeOpening && sheet.setChecks(checks),
      () => opening === this.resumeOpening && sheet.setChecks('error'),
    );
    const answer = await opened;
    if (answer === null) return;
    if (!(await this.stepUp.ensure('Resume trading'))) return;
    this.busyId.set(h.id);
    try {
      // A stale second factor comes back as 403 step_up_required: the session
      // interceptor prompts for a code and retries once.
      await this.api.resume(h.id, {
        confirmation: RESUME_CONFIRMATION,
        reason: answer.reason,
        ...(answer.overrideChecks ? { override_checks: true } : {}),
      });
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
