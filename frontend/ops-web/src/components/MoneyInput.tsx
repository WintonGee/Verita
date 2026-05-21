import { useState } from 'react';
import { parseDollarsToMicroCents } from '../lib/money';

interface MoneyInputProps {
  /** current value in integer micro_cents */
  valueMicroCents: number;
  /** called with the parsed integer micro_cents (NaN if input is empty/invalid) */
  onChangeMicroCents: (microCents: number) => void;
  id?: string;
  autoFocus?: boolean;
}

/**
 * Dollar-denominated text input that keeps the canonical value as integer
 * micro_cents. The displayed text is the only float-ish thing; parsing rounds
 * to an integer immediately. No float arithmetic is retained internally.
 */
export function MoneyInput({
  valueMicroCents,
  onChangeMicroCents,
  id,
  autoFocus,
}: MoneyInputProps) {
  const [text, setText] = useState(() =>
    Number.isFinite(valueMicroCents) && valueMicroCents !== 0
      ? (valueMicroCents / 1e8).toFixed(2)
      : '',
  );

  return (
    <div className="flex items-center rounded border border-gray-300 focus-within:border-blue-500">
      <span className="px-2 text-gray-500">$</span>
      <input
        id={id}
        type="text"
        inputMode="decimal"
        autoFocus={autoFocus}
        placeholder="0.00"
        value={text}
        onChange={(e) => {
          const cleaned = e.target.value.replace(/[^0-9.]/g, '');
          setText(cleaned);
          onChangeMicroCents(parseDollarsToMicroCents(cleaned));
        }}
        className="w-full rounded-r px-2 py-2 outline-none"
      />
    </div>
  );
}
