import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  linkedSignal,
  output,
  signal,
} from '@angular/core';

/**
 * Live JSON of the spec being built. Read-only by default; the "Edit JSON"
 * toggle makes it editable, and "Apply" parses the text and hands the object
 * back to the builder (which then re-renders from it).
 */
@Component({
  selector: 'app-spec-json',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="bar">
      <label class="check">
        <input
          type="checkbox"
          [checked]="editing()"
          (change)="setEditing($any($event.target).checked)"
        />
        Edit JSON
      </label>
      @if (editing()) {
        <div class="actions">
          <button type="button" class="btn" [disabled]="!dirty()" (click)="revert()">Revert</button>
          <button type="button" class="btn btn-primary" [disabled]="!dirty()" (click)="applyText()">
            Apply
          </button>
        </div>
      }
    </div>
    <label class="visually-hidden" for="spec-json-text">Rule spec as JSON</label>
    <textarea
      id="spec-json-text"
      class="json"
      spellcheck="false"
      autocapitalize="off"
      autocomplete="off"
      [class.editing]="editing()"
      [readOnly]="!editing()"
      [attr.aria-invalid]="parseError() ? true : null"
      [attr.aria-describedby]="parseError() ? 'spec-json-error' : null"
      [value]="text()"
      (input)="text.set($any($event.target).value)"
    ></textarea>
    @if (parseError(); as e) {
      <p id="spec-json-error" class="error" role="alert">{{ e }}</p>
    } @else if (editing()) {
      <p class="hint">Apply to load the JSON into the builder; the API validates it there.</p>
    }
  `,
  styles: `
    :host {
      display: grid;
      gap: var(--space-2);
      min-width: 0;
    }
    .bar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: var(--space-2);
    }
    .actions {
      display: flex;
      gap: var(--space-2);
    }
    .json {
      width: 100%;
      min-height: 22rem;
      max-height: 70vh;
      padding: var(--space-3);
      border: 1px solid var(--color-border);
      border-radius: var(--radius-sm);
      background: var(--color-surface-2);
      color: var(--color-ink);
      font-family: var(--font-mono);
      font-size: var(--text-xs);
      line-height: 1.55;
      white-space: pre;
      overflow: auto;
      resize: vertical;
      tab-size: 2;
    }
    .json.editing {
      background: var(--color-surface);
      border-color: var(--color-border-strong);
    }
    .json[aria-invalid='true'] {
      border-color: var(--color-loss);
    }
    .error {
      font-size: var(--text-xs);
      color: var(--color-loss);
      overflow-wrap: anywhere;
    }
    .hint {
      font-size: var(--text-xs);
      color: var(--color-ink-3);
    }
  `,
})
export class SpecJson {
  readonly spec = input.required<unknown>();
  /** A parsed JSON object the trader applied. */
  readonly apply = output<Record<string, unknown>>();

  protected readonly editing = signal(false);
  private readonly pretty = computed(() => JSON.stringify(this.spec(), null, 2));
  protected readonly text = linkedSignal(() => this.pretty());
  protected readonly parseError = signal<string | null>(null);
  protected readonly dirty = computed(() => this.text() !== this.pretty());

  protected setEditing(on: boolean): void {
    this.editing.set(on);
    if (!on) this.revert();
  }

  protected revert(): void {
    this.text.set(this.pretty());
    this.parseError.set(null);
  }

  protected applyText(): void {
    let parsed: unknown;
    try {
      parsed = JSON.parse(this.text());
    } catch (e) {
      this.parseError.set(`Not valid JSON: ${e instanceof Error ? e.message : String(e)}`);
      return;
    }
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
      this.parseError.set('A rule spec must be a JSON object.');
      return;
    }
    this.parseError.set(null);
    this.apply.emit(parsed as Record<string, unknown>);
  }
}
