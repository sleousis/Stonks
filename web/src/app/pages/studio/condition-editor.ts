import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import {
  COMPARISON_OPS,
  type CompareNode,
  type ComparisonOp,
  type ConditionNode,
  type GroupNode,
  type GroupType,
  type IssueMap,
  type NotNode,
  type Operand,
  addCondition,
  constantOperand,
  defaultComparison,
  indicatorOperand,
  negate,
  removeCondition,
  replaceCondition,
  setGroupType,
  toGroup,
} from './rule-spec';

const MAX_CHILDREN = 32;
const MAX_DEPTH = 16;

/**
 * One node of an entry / exit condition tree: a comparison, an all / any
 * group, or a NOT. Recursive; every edit emits a new node to the parent, so
 * the spec stays immutable. `path` is the API's dotted path to this node
 * (`entry.conditions[1]`) and picks the validation issues shown here.
 */
@Component({
  selector: 'app-condition-editor',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { '[attr.data-node]': 'path()' },
  templateUrl: './condition-editor.html',
  styleUrl: './condition-editor.scss',
})
export class ConditionEditor {
  readonly node = input.required<ConditionNode>();
  readonly path = input.required<string>();
  readonly indicatorIds = input.required<readonly string[]>();
  readonly issues = input<IssueMap>({});
  /** Root nodes cannot be removed, only replaced. */
  readonly removable = input(true);
  readonly depth = input(1);

  readonly nodeChange = output<ConditionNode>();
  readonly remove = output<void>();

  protected readonly ops = COMPARISON_OPS;

  protected readonly compareNode = computed(() =>
    this.node().type === 'compare' ? (this.node() as CompareNode) : null,
  );
  protected readonly groupNode = computed(() => {
    const t = this.node().type;
    return t === 'all' || t === 'any' ? (this.node() as GroupNode) : null;
  });
  protected readonly notNode = computed(() =>
    this.node().type === 'not' ? (this.node() as NotNode) : null,
  );
  protected readonly canNest = computed(() => this.depth() < MAX_DEPTH - 1);
  protected readonly isRoot = computed(() => !this.removable());

  /** Messages for this node itself (e.g. "at least one side must be an indicator"). */
  protected readonly nodeIssues = computed(() => this.issues()[this.path()] ?? []);

  protected issuesAt(suffix: string): readonly string[] {
    return this.issues()[`${this.path()}.${suffix}`] ?? [];
  }

  protected compareIssues(): string[] {
    return [
      ...this.nodeIssues(),
      ...['left.id', 'left.value', 'left.type', 'op', 'right.id', 'right.value', 'right.type']
        .flatMap((s) => this.issuesAt(s))
        .map((m) => m),
    ];
  }

  protected operandKey(o: Operand | undefined): string {
    if (!o) return '';
    return o.type === 'constant' ? 'const' : `i:${o.id}`;
  }

  /** Indicator ids to offer, including an unknown one the operand already points at. */
  protected operandOptions(o: Operand | undefined): string[] {
    const ids = [...this.indicatorIds()];
    if (o?.type === 'indicator' && !ids.includes(o.id)) ids.push(o.id);
    return ids;
  }

  protected childPath(index: number): string {
    return `${this.path()}.conditions[${index}]`;
  }

  // ---- comparison edits ----------------------------------------------------

  protected setOperand(side: 'left' | 'right', key: string): void {
    const c = this.compareNode();
    if (!c) return;
    const prev = c[side];
    const next: Operand =
      key === 'const'
        ? constantOperand(prev.type === 'constant' ? prev.value : 0)
        : indicatorOperand(key.slice(2));
    this.nodeChange.emit({ ...c, [side]: next });
  }

  protected setConstant(side: 'left' | 'right', raw: string): void {
    const c = this.compareNode();
    if (!c) return;
    const value = raw.trim() === '' ? Number.NaN : Number(raw);
    // Keep NaN out of the JSON: an empty box stays 0 until a number is typed.
    this.nodeChange.emit({ ...c, [side]: constantOperand(Number.isFinite(value) ? value : 0) });
  }

  protected setOp(op: string): void {
    const c = this.compareNode();
    if (c) this.nodeChange.emit({ ...c, op: op as ComparisonOp });
  }

  protected constantValue(o: Operand): number {
    return o.type === 'constant' ? o.value : 0;
  }

  // ---- structure edits -----------------------------------------------------

  protected setType(type: string): void {
    const g = this.groupNode();
    if (g) this.nodeChange.emit(setGroupType(g, type as GroupType));
  }

  protected addComparison(): void {
    const g = this.groupNode();
    if (g) this.nodeChange.emit(addCondition(g, defaultComparison(this.indicatorIds())));
  }

  protected addGroup(): void {
    const g = this.groupNode();
    if (!g) return;
    const inner: GroupNode = { type: 'any', conditions: [defaultComparison(this.indicatorIds())] };
    this.nodeChange.emit(addCondition(g, inner));
  }

  /** Root comparison: wrap it in an "all of" group and add a second comparison. */
  protected addSibling(): void {
    const g = addCondition(toGroup(this.node()), defaultComparison(this.indicatorIds()));
    this.nodeChange.emit(g);
  }

  protected childChanged(index: number, child: ConditionNode): void {
    const g = this.groupNode();
    if (g) this.nodeChange.emit(replaceCondition(g, index, child));
  }

  protected childRemoved(index: number): void {
    const g = this.groupNode();
    if (!g) return;
    const next = removeCondition(g, index);
    // A group can't be empty; the last child leaving takes the group with it
    // (or, at the root, leaves a fresh comparison).
    if (next.conditions.length) this.nodeChange.emit(next);
    else if (this.removable()) this.remove.emit();
    else this.nodeChange.emit(defaultComparison(this.indicatorIds()));
  }

  protected negate(): void {
    this.nodeChange.emit(negate(this.node()));
  }

  protected unwrapNot(): void {
    const n = this.notNode();
    if (n) this.nodeChange.emit(n.condition);
  }

  protected innerChanged(child: ConditionNode): void {
    const n = this.notNode();
    if (n) this.nodeChange.emit({ ...n, condition: child });
  }

  protected canAddChild(g: GroupNode): boolean {
    return g.conditions.length < MAX_CHILDREN;
  }
}
