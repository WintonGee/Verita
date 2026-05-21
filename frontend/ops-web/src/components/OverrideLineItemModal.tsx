import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Modal } from './Modal';
import { MoneyInput } from './MoneyInput';
import { apiFetch, ApiError } from '../lib/apiClient';
import { formatUSD, formatSignedUSD } from '../lib/money';
import type { InvoiceLineItem, LineItemOverrideResponse } from '../types';

const MIN_REASON_LEN = 10;

interface OverrideLineItemModalProps {
  customerId: string;
  invoiceId: string;
  lineItem: InvoiceLineItem;
  onClose: () => void;
  onSuccess: (msg: string) => void;
}

export function OverrideLineItemModal({
  customerId,
  invoiceId,
  lineItem,
  onClose,
  onSuccess,
}: OverrideLineItemModalProps) {
  const queryClient = useQueryClient();

  const currentMicroCents = lineItem.amount_micro_cents;
  const [newMicroCents, setNewMicroCents] = useState(currentMicroCents);
  const [description, setDescription] = useState(lineItem.description ?? '');
  const [reason, setReason] = useState('');

  // Same idempotency discipline as the credit modal: one key per modal life.
  const [idempotencyKey] = useState(() => crypto.randomUUID());

  // amount_micro_cents >= 0 allowed for overrides (per contract)
  const amountValid = Number.isInteger(newMicroCents) && newMicroCents >= 0;
  const reasonValid = reason.trim().length >= MIN_REASON_LEN;
  const changed = amountValid && newMicroCents !== currentMicroCents;
  const canSubmit = amountValid && reasonValid && changed;

  const fromLabel = formatUSD(currentMicroCents);
  const toLabel = amountValid ? formatUSD(newMicroCents) : '$0.00';
  const diffLabel = amountValid
    ? formatSignedUSD(newMicroCents - currentMicroCents)
    : '';

  const mutation = useMutation({
    mutationFn: () =>
      apiFetch<LineItemOverrideResponse>(
        `/ops/invoices/${invoiceId}/line-items/${lineItem.id}`,
        {
          method: 'PATCH',
          headers: { 'Idempotency-Key': idempotencyKey },
          body: JSON.stringify({
            amount_micro_cents: newMicroCents,
            description: description.trim() || undefined,
            reason: reason.trim(),
          }),
        },
      ),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['customer', customerId] });
      onSuccess(
        `Line item overridden to ${formatUSD(data.amount_micro_cents)}.`,
      );
      onClose();
    },
  });

  const errMessage =
    mutation.error instanceof ApiError
      ? mutation.error.message
      : mutation.error instanceof Error
        ? mutation.error.message
        : null;

  return (
    <Modal title="Override line item" onClose={onClose}>
      <div className="space-y-4">
        <div className="rounded border border-gray-200 bg-gray-50 p-3 text-sm">
          <div className="text-gray-500">Current amount</div>
          <div className="text-lg font-semibold text-gray-900">{fromLabel}</div>
          <div className="mt-1 text-gray-600">{lineItem.description}</div>
        </div>

        <div>
          <label
            htmlFor="override-amount"
            className="mb-1 block text-sm font-medium text-gray-700"
          >
            New amount
          </label>
          <MoneyInput
            id="override-amount"
            autoFocus
            valueMicroCents={newMicroCents}
            onChangeMicroCents={setNewMicroCents}
          />
        </div>

        <div>
          <label
            htmlFor="override-description"
            className="mb-1 block text-sm font-medium text-gray-700"
          >
            Description{' '}
            <span className="font-normal text-gray-400">(optional)</span>
          </label>
          <input
            id="override-description"
            type="text"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            className="w-full rounded border border-gray-300 px-2 py-2 text-sm outline-none focus:border-blue-500"
          />
        </div>

        <div>
          <label
            htmlFor="override-reason"
            className="mb-1 block text-sm font-medium text-gray-700"
          >
            Reason{' '}
            <span className="font-normal text-gray-400">
              (min {MIN_REASON_LEN} chars)
            </span>
          </label>
          <textarea
            id="override-reason"
            rows={3}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            className="w-full rounded border border-gray-300 px-2 py-2 text-sm outline-none focus:border-blue-500"
            placeholder="Why is this override being made? (audited)"
          />
          {!reasonValid && reason.length > 0 && (
            <p className="mt-1 text-xs text-amber-600">
              {MIN_REASON_LEN - reason.trim().length} more characters required.
            </p>
          )}
        </div>

        {/* Confirmation restatement — live diff */}
        <div className="rounded border border-gray-200 bg-gray-50 p-3 text-sm text-gray-700">
          <p>
            This will change the line item from{' '}
            <span className="font-semibold">{fromLabel}</span> to{' '}
            <span className="font-semibold">{toLabel}</span>
            {amountValid && newMicroCents !== currentMicroCents && (
              <>
                {' '}
                (<span className="font-semibold">{diffLabel}</span>)
              </>
            )}{' '}
            and write an audit entry.
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
            className="rounded bg-amber-600 px-4 py-2 text-sm font-semibold text-white hover:bg-amber-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {mutation.isPending
              ? 'Applying…'
              : `Change from ${fromLabel} to ${toLabel}`}
          </button>
        </div>
      </div>
    </Modal>
  );
}
