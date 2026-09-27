import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  output,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type { IntervalInfo, StrategyClassInfo, SweepRequest, UniverseView } from '../../api/models';
import { SessionService } from '../../core/auth/session.service';
import { PermissionNote } from '../../shared/ui/permission-note';
import { SUITES } from './lab-requests';
import {
  type SweepForm,
  buildSweepRequest,
  defaultSweepForm,
  sweepErrors,
} from './research-requests';

/** Pick a basket, the strategies and the suite for a sweep; emits the request body. */
@Component({
  selector: 'app-sweep-form',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PermissionNote, RouterLink],
  templateUrl: './sweep-form.html',
  styleUrls: ['./lab-form.scss', './research-form.scss'],
})
export class SweepFormView {
  readonly classes = input.required<readonly StrategyClassInfo[]>();
  readonly intervals = input<readonly IntervalInfo[]>([]);
  readonly universes = input<readonly UniverseView[]>([]);
  readonly busy = input(false);
  readonly submitted = output<SweepRequest>();

  private readonly session = inject(SessionService);
  protected readonly canRun = computed(() => this.session.can('lab.run'));

  protected readonly suites = SUITES.filter((s) => s.id !== 'custom');
  protected readonly form = signal<SweepForm>(defaultSweepForm());
  protected readonly tried = signal(false);

  private readonly allErrors = computed(() => sweepErrors(this.form()));
  protected readonly errors = computed(() => (this.tried() ? this.allErrors() : {}));
  protected readonly errorCount = computed(() => Object.keys(this.errors()).length);
  protected readonly suiteHint = computed(
    () => SUITES.find((s) => s.id === this.form().suite)?.description ?? '',
  );
  protected readonly strategiesNote = computed(() => {
    const n = this.form().strategies.length;
    return n ? `${n} picked` : `all ${this.classes().length}`;
  });

  protected intervalOptions(): readonly IntervalInfo[] {
    const list = this.intervals();
    return list.length ? list : [{ code: '1d', is_intraday: false, seconds: 86_400 }];
  }

  protected patch(p: Partial<SweepForm>): void {
    this.form.update((f) => ({ ...f, ...p }));
  }

  protected setBudget(raw: string): void {
    const n = raw.trim() === '' ? null : Number(raw);
    this.patch({ budget: n === null || Number.isNaN(n) ? null : n });
  }

  protected toggleStrategy(classPath: string, on: boolean): void {
    this.form.update((f) => ({
      ...f,
      strategies: on
        ? [...f.strategies.filter((s) => s !== classPath), classPath]
        : f.strategies.filter((s) => s !== classPath),
    }));
  }

  protected submit(): void {
    if (!this.canRun()) return;
    this.tried.set(true);
    if (Object.keys(this.allErrors()).length) return;
    this.submitted.emit(buildSweepRequest(this.form()));
  }
}
