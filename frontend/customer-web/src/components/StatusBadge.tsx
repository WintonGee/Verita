interface StatusBadgeProps {
  status: string;
}

const COLORS: Record<string, string> = {
  paid: 'bg-green-100 text-green-800 border-green-300',
  issued: 'bg-blue-100 text-blue-800 border-blue-300',
  draft: 'bg-gray-100 text-gray-700 border-gray-300',
  void: 'bg-gray-100 text-gray-500 border-gray-300 line-through',
  overdue: 'bg-red-100 text-red-800 border-red-300',
  active: 'bg-green-100 text-green-800 border-green-300',
  suspended: 'bg-red-100 text-red-800 border-red-300',
};

export function StatusBadge({ status }: StatusBadgeProps) {
  const cls =
    COLORS[status.toLowerCase()] ?? 'bg-gray-100 text-gray-700 border-gray-300';
  return (
    <span
      className={`inline-block rounded-full border px-2 py-0.5 text-xs font-medium capitalize ${cls}`}
    >
      {status}
    </span>
  );
}
