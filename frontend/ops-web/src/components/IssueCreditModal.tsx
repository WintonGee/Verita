import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Modal } from './Modal';
import { MoneyInput } from './MoneyInput';
import { apiFetch, ApiError } from '../lib/apiClient';
import { formatUSD } from '../lib/money';
import type { CreditResponse } from '../types';

const MIN_REASON_LEN = 10;

interface IssueCreditModalProps {
  customerId: string;
  customerName: string;
  onClose: () => void;
  onSuccess: (msg: string) => void;
}

export function IssueCreditModal({
  customerId,
  customerName,
  onClose,
  onSuccess,
}: IssueCreditModalProps) {
  const queryClient = useQueryClient();

  const [amountMicroCents, setAmountMicroCents] = useState(NaN);
  const [reason, setReason] = useState('');

  // Generate the Idempotency-Key ONCE for the lifetime of this modal (lazy
  // initializer — must not regenerate per render). Retries reuse the same key
  // so a network retry can never double-issue the credit.
  const [idempotencyKey] = useState(() => crypto.randomUUID());

  const amountValid = Number.isInteger(amountMicroCents) && amountMicroCents > 0;
  const reasonValid = reason.trim().length >= MIN_REASON_LEN;
  const canSubmit = amountValid && reasonValid;

  const amountLabel = amountValid ? formatUSD(amountMicroCents) : '$0.00';

  const mutation = useMutation({
    mutationFn: () =>
      apiFetch<CreditResponse>(`/ops/customers/${customerId}/credits`, {
        method: 'POST',
        headers: { 'Idempotency-Key': idempotencyKey },
        body: JSON.stringify({
          amount_micro_cents: amountMicroCents,
          reason: reason.trim(),
        }),
      }),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['customer', customerId] });
      onSuccess(`Credit of ${formatUSD(data.amount_micro_cents)} issued.`);
      onClose();
    },
    // On error we deliberately keep the modal open AND keep the same
    // idempotencyKey, so the user can retry safely.
  });

  const errMessage =
    mutation.error instanceof ApiError
      ? mutation.error.message
      : mutation.error instanceof Error
        ? mutation.error.message
        : null;

  return (
    <Modal title={`Issue credit to ${customerName}`} onClose={onClose}>
      <div className="space-y-4">
        <div>
          <label
            htmlFor="credit-amount"
            className="mb-1 block text-sm font-medium text-gray-700"
          >
            Amount
          </label>
          <MoneyInput
            id="credit-amount"
            autoFocus
            valueMicroCents={amountMicroCents}
            onChangeMicroCents={setAmountMicroCents}
          />
        </div>

        <div>
          <label
            htmlFor="credit-reason"
            className="mb-1 block text-sm font-medium text-gray-700"
          >
            Reason{' '}
            <span className="font-normal text-gray-400">
              (min {MIN_REASON_LEN} chars)
            </span>
          </label>
          <textarea
            id="credit-reason"
            rows={3}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            className="w-full rounded border border-gray-300 px-2 py-2 text-sm outline-none focus:border-blue-500"
            placeholder="Why is this credit being issued? (audited)"
          />
          {!reasonValid && reason.length > 0 && (
            <p className="mt-1 text-xs text-amber-600">
              {MIN_REASON_LEN - reason.trim().length > 0
                ? `${MIN_REASON_LEN - reason.trim().length} more characters required.`
                : 'Reason required.'}
            </p>
          )}
        </div>

        {/* Confirmation restatement — reflects live state */}
        <div className="rounded border border-gray-200 bg-gray-50 p-3 text-sm text-gray-700">
          <p>
            This will create a credit of{' '}
            <span className="font-semibold">{amountLabel}</span> and write an
            audit entry.
          </p>
          <p className="mt-1 font-mono text-xs text-gray-500">
            Idempotency-Key: {idempotencyKey}
          </p>
        </div>

        {errMessage && (
          <div
            className="rounded border border-red-300 bg-red-50 p-3 text-sm text-red-800"
            role="alert"
          >
            {errMessage}{' '}
            <span className="text-red-600">
              (you can retry — same key is reused)
            </span>
          </div>
        )}

        <div className="flex justify-end gap-2 pt-1">
          <button
            type="button"
            onClick={onClose}
            disabled={mutation.isPending}
            className="rounded border border-gray-300 px-4 py-2 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => mutation.mutate()}
            disabled={!canSubmit || mutation.isPending}
            className="rounded bg-green-700 px-4 py-2 text-sm font-semibold text-white hover:bg-green-800 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {mutation.isPending
              ? 'Issuing…'
              : `Confirm — issue ${amountLabel}`}
          </button>
        </div>
      </div>
    </Modal>
  );
}
