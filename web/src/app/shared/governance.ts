import type { GoLiveReport, StatusChangeRequest } from '../api/models';
import { ApiError, errorMessage } from '../core/http/api-error';
import type { ToastService } from '../core/notify/toast.service';
import { OVERRIDE_MIN_REASON, type StatusChangeDialog } from './ui/status-change-dialog';

/** The go-live gate refused a promotion (the API answers 409). */
export function isGoLiveRefusal(err: unknown): err is ApiError {
  return err instanceof ApiError && err.status === 409;
}

export interface PromotionSteps<T> {
  /** Strategy id, named in messages. */
  id: string;
  dialog: StatusChangeDialog;
  toasts: ToastService;
  /** The go-live report shown before asking. */
  golive: () => Promise<GoLiveReport>;
  /** The promote call, made silently: this flow reports its own errors. */
  promote: (body: StatusChangeRequest) => Promise<T>;
  /** Wording for the first dialog. */
  title: string;
  message: string;
  confirmLabel: string;
  /** Called with `true` while a request runs. */
  busy?: (on: boolean) => void;
}

/**
 * Promote with the go-live gate in front:
 * 1. load the go-live report and show it with a reason field and a
 *    hold-to-confirm button (no typing for a promotion the gate allows);
 * 2. promote; on a 409 (gate refused) show the failing checks and offer an
 *    override that needs a reason of at least 20 characters and the typed
 *    word "override";
 * 3. promote again with `override: true`.
 * Resolves to the API's answer, or `null` when cancelled or failed (other
 * errors are toasted here).
 */
export async function promoteThroughGate<T>(steps: PromotionSteps<T>): Promise<T | null> {
  const { id, dialog, toasts } = steps;
  let report: GoLiveReport | null = null;
  let note: string | null = null;
  steps.busy?.(true);
  try {
    report = await steps.golive();
  } catch (err) {
    note = `Could not run the go-live check first (${errorMessage(err)}). The API still applies it when you promote.`;
  } finally {
    steps.busy?.(false);
  }

  const body = await dialog.open({
    title: steps.title,
    message: steps.message,
    confirmLabel: steps.confirmLabel,
    minReason: 1,
    reasonHint: 'Why now? Kept in the status history.',
    hold: true,
    golive: report,
    goliveNote: note,
  });
  if (!body) return null;

  let refusal: ApiError;
  steps.busy?.(true);
  try {
    return await steps.promote(body);
  } catch (err) {
    if (!isGoLiveRefusal(err)) {
      toasts.error(errorMessage(err), err instanceof ApiError ? err.title : undefined);
      return null;
    }
    refusal = err;
  } finally {
    steps.busy?.(false);
  }

  const override = await dialog.open({
    title: `The go-live gate refused ${id}`,
    message:
      `${refusal.message} Going live anyway puts it on real orders without the evidence the ` +
      'gate asks for. The override and your reason are recorded.',
    confirmLabel: 'Override and go live',
    tone: 'danger',
    minReason: OVERRIDE_MIN_REASON,
    typedConfirmation: 'override',
    override: true,
    golive: report,
  });
  if (!override) return null;

  steps.busy?.(true);
  try {
    return await steps.promote(override);
  } catch (err) {
    toasts.error(errorMessage(err), err instanceof ApiError ? err.title : undefined);
    return null;
  } finally {
    steps.busy?.(false);
  }
}
