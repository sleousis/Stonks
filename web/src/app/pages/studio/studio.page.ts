import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  Injector,
  afterNextRender,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';
import { Router, RouterLink } from '@angular/router';

import type { Draft, DraftCreate } from '../../api/models';
import { StudioService } from '../../api/studio.service';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { ApiError } from '../../core/http/api-error';
import { ToastService } from '../../core/notify/toast.service';
import { AgoPipe } from '../../shared/format.pipes';
import { PageHeader } from '../../shared/ui/page-header';
import { PermissionNote } from '../../shared/ui/permission-note';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import { blankSpec } from './rule-spec';

const PAGE_SIZE = 100;
const BLANK = 'blank';
const CODE = 'code';

export const CODE_STARTER = `from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from stonks.core.params import ParameterSpec
from stonks.core.types import Order, Portfolio
from stonks.strategies.base import BaseStrategy


class MyStrategy(BaseStrategy):
    id = "my_strategy"

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="ticker",
                kind="categorical",
                default="AAPL.US",
                bounds=None,
                tunable=False,
                description="Ticker to hold.",
            ),
        ]

    def estimate_return(self, ticker: str, as_of: date, lake: Any) -> float | None:
        return 1.0 if ticker == self.params["ticker"] else None

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: date,
    ) -> list[Order]:
        return []
`;

/** Starting points offered when creating a draft. */
interface StartOption {
  id: string;
  title: string;
  description: string;
}

/**
 * Strategy Studio landing page: the drafts list, and creating a draft from a
 * template, a blank rule set or (when the operator allows it) Python code.
 */
@Component({
  selector: 'app-studio-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    PageHeader,
    PermissionNote,
    StatusPill,
    LoadingState,
    EmptyState,
    ErrorState,
    AgoPipe,
  ],
  templateUrl: './studio.page.html',
  styleUrl: './studio.page.scss',
})
export class StudioPage {
  private readonly studio = inject(StudioService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);
  private readonly router = inject(Router);
  private readonly session = inject(SessionService);
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly injector = inject(Injector);

  /** Creating, renaming and deleting drafts need `lab.run`. */
  protected readonly canLab = computed(() => this.session.can('lab.run'));

  protected readonly drafts = resource({
    loader: () => this.studio.drafts({ limit: PAGE_SIZE }),
  });
  protected readonly templates = resource({ loader: () => this.studio.templates() });
  protected readonly capabilities = resource({ loader: () => this.studio.capabilities() });

  // ---- create ----------------------------------------------------------------
  protected readonly creating = signal(false);
  protected readonly createBusy = signal(false);
  protected readonly newName = signal('');
  protected readonly start = signal<string>(BLANK);
  protected readonly nameTouched = signal(false);
  /** Set when the API refused a code draft (403), e.g. a server without the capabilities route. */
  protected readonly codeRefused = signal<string | null>(null);

  protected readonly codeDisabled = computed(() => {
    if (this.codeRefused()) return true;
    if (this.capabilities.hasValue()) return !this.capabilities.value().code_strategies;
    if (!this.drafts.hasValue()) return false;
    return this.drafts.value().items.some((d) => d.kind === 'code' && d.source_code === null);
  });

  protected readonly options = computed<StartOption[]>(() => {
    const templates = this.templates.hasValue() ? this.templates.value() : [];
    return [
      {
        id: BLANK,
        title: 'Blank rules',
        description: 'Close above its 50-bar average; build from there.',
      },
      ...templates.map((t) => ({ id: `t:${t.id}`, title: t.title, description: t.description })),
      {
        id: CODE,
        title: 'Python code',
        description: this.codeDisabled()
          ? 'A BaseStrategy subclass. Turned off on this server.'
          : 'A BaseStrategy subclass, run inside the server.',
      },
    ];
  });

  protected readonly nameError = computed(() =>
    this.nameTouched() && !this.newName().trim() ? 'Give the draft a name.' : null,
  );

  protected openCreate(): void {
    if (!this.canLab()) return;
    this.creating.set(true);
    this.nameTouched.set(false);
  }

  protected pick(option: StartOption): void {
    this.start.set(option.id);
    if (!this.newName().trim() && option.id !== BLANK && option.id !== CODE) {
      this.newName.set(option.title);
    }
  }

  /** The create request for the chosen starting point. */
  buildCreate(): DraftCreate {
    const name = this.newName().trim();
    const choice = this.start();
    if (choice === CODE) return { name, kind: 'code', source_code: CODE_STARTER, spec: {} };
    if (choice.startsWith('t:')) {
      const t = this.templates.hasValue()
        ? this.templates.value().find((x) => `t:${x.id}` === choice)
        : undefined;
      if (t) return { name, kind: 'rule', spec: { ...t.spec, name } };
    }
    return { name, kind: 'rule', spec: blankSpec(name) as Record<string, unknown> };
  }

  async create(): Promise<void> {
    this.nameTouched.set(true);
    if (!this.newName().trim() || this.createBusy() || !this.canLab()) return;
    this.createBusy.set(true);
    try {
      const draft = await this.studio.create(this.buildCreate());
      this.toasts.success(`Created ${draft.name}.`);
      this.creating.set(false);
      this.newName.set('');
      await this.router.navigate(['/studio', draft.id]);
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) this.codeRefused.set(e.message);
    } finally {
      this.createBusy.set(false);
    }
  }

  // ---- rename / delete -------------------------------------------------------
  protected readonly renamingId = signal<string | null>(null);
  protected readonly renameText = signal('');

  protected startRename(d: Draft): void {
    if (!this.canLab()) return;
    this.renamingId.set(d.id);
    this.renameText.set(d.name);
    this.focusAfterRender(`#rename-${CSS.escape(d.id)}`);
  }

  /** Close the rename form and give focus back to that draft's Rename button. */
  protected cancelRename(): void {
    const id = this.renamingId();
    this.renamingId.set(null);
    if (id) this.focusAfterRender(`[data-rename-for="${CSS.escape(id)}"]`);
  }

  private focusAfterRender(selector: string): void {
    afterNextRender(() => this.host.nativeElement.querySelector<HTMLElement>(selector)?.focus(), {
      injector: this.injector,
    });
  }

  async rename(d: Draft): Promise<void> {
    const name = this.renameText().trim();
    if (!name) return;
    if (name === d.name) {
      this.cancelRename();
      return;
    }
    try {
      await this.studio.update(d.id, { name });
      this.toasts.success(`Renamed to ${name}.`);
      this.cancelRename();
      this.drafts.reload();
    } catch {
      // The error interceptor already showed the API's message.
    }
  }

  protected readonly deletingId = signal<string | null>(null);

  async remove(d: Draft): Promise<void> {
    if (!this.canLab()) return;
    const ok = await this.confirm.confirm({
      title: `Delete ${d.name}?`,
      message:
        d.status === 'registered'
          ? `The draft is removed. The strategy started from it (${d.registered_strategy_id}) stays in Strategies.`
          : 'The draft and its rules are removed. This cannot be undone.',
      confirmLabel: 'Delete',
      tone: 'danger',
    });
    if (!ok) return;
    this.deletingId.set(d.id);
    try {
      await this.studio.delete(d.id);
      this.toasts.success(`Deleted ${d.name}.`);
      this.drafts.reload();
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.deletingId.set(null);
    }
  }

  protected summary(d: Draft): string {
    if (d.kind === 'code') return 'Python code';
    const spec = d.spec as { indicators?: unknown[]; universe?: { asset_classes?: string[] } };
    const n = Array.isArray(spec.indicators) ? spec.indicators.length : 0;
    const classes = spec.universe?.asset_classes?.join(', ') || 'equity';
    return `${n} ${n === 1 ? 'indicator' : 'indicators'} · ${classes}`;
  }
}
