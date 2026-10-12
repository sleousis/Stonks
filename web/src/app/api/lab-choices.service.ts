import { Injectable, inject, signal, untracked } from '@angular/core';

import { LabService } from './lab.service';
import type { ObjectiveInfo, TunerInfo } from './models';

/**
 * The objectives and tuners a lab run can name, read once from the API's
 * registries (`GET /api/lab/objectives`, `GET /api/lab/tuners`) and shared
 * by every page. A new objective or tuner on the server (a plugin too)
 * shows up here with its label, with no console change.
 *
 * Reading a list starts its load. Until it arrives, or when it fails, the
 * list is empty and a label falls back to the name.
 */
@Injectable({ providedIn: 'root' })
export class LabChoices {
  private readonly lab = inject(LabService);
  private readonly objectiveList = signal<readonly ObjectiveInfo[] | null>(null);
  private readonly tunerList = signal<readonly TunerInfo[] | null>(null);
  private objectivesAsked = false;
  private tunersAsked = false;

  /** Every objective in the server's menu order ([] until loaded). */
  objectives(): readonly ObjectiveInfo[] {
    if (!this.objectivesAsked) {
      this.objectivesAsked = true;
      untracked(() => this.lab.objectives()).then(
        (list) => this.objectiveList.set(list),
        () => this.objectiveList.set([]),
      );
    }
    return this.objectiveList() ?? [];
  }

  /** Every tuner ([] until loaded). */
  tuners(): readonly TunerInfo[] {
    if (!this.tunersAsked) {
      this.tunersAsked = true;
      untracked(() => this.lab.tuners()).then(
        (list) => this.tunerList.set(list),
        () => this.tunerList.set([]),
      );
    }
    return this.tunerList() ?? [];
  }

  /** The label of objective `name`, or `null` when the server has none. */
  objectiveLabel(name: string): string | null {
    return this.objectives().find((o) => o.name === name)?.label ?? null;
  }

  /** The tuner named `name`, or `null`. */
  tuner(name: string): TunerInfo | null {
    return this.tuners().find((t) => t.name === name) ?? null;
  }
}
