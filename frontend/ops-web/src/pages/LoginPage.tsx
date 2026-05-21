import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { login } from '../lib/auth';
import { apiFetch, ApiError } from '../lib/apiClient';
import type { MeResponse } from '../types';
import { ME_QUERY_KEY } from '../lib/auth';

// Demo staff login, created by `manage.py seed`. Shown on the page for easy
// testing; demo-only convenience, not production code.
const DEMO_USERNAME = 'ops';
const DEMO_PASSWORD = 'ops-pass-123';

export function LoginPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await login(username, password);
      // GET /ops/auth/me sets the csrftoken cookie required for later mutations,
      // and seeds the auth bootstrap query.
      const me = await apiFetch<MeResponse>('/ops/auth/me');
      queryClient.setQueryData(ME_QUERY_KEY, me);
      navigate('/customers', { replace: true });
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError('Invalid username or password.');
      } else if (err instanceof Error) {
        setError(err.message);
      } else {
        setError('Login failed.');
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-gray-100">
      <form
        onSubmit={handleSubmit}
        className="w-full max-w-sm space-y-4 rounded-lg bg-white p-6 shadow"
      >
        <div>
          <h1 className="text-xl font-semibold text-gray-900">
            Verita Ops Console
          </h1>
          <p className="mt-1 text-sm text-gray-500">Staff sign in</p>
        </div>

        <div>
          <label
            htmlFor="username"
            className="mb-1 block text-sm font-medium text-gray-700"
          >
            Username
          </label>
          <input
            id="username"
            type="text"
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            className="w-full rounded border border-gray-300 px-3 py-2 text-sm outline-none focus:border-blue-500"
          />
        </div>

        <div>
          <label
            htmlFor="password"
            className="mb-1 block text-sm font-medium text-gray-700"
          >
            Password
          </label>
          <input
            id="password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="w-full rounded border border-gray-300 px-3 py-2 text-sm outline-none focus:border-blue-500"
          />
        </div>

        {error && (
          <div
            className="rounded border border-red-300 bg-red-50 p-2 text-sm text-red-800"
            role="alert"
          >
            {error}
          </div>
        )}

        <button
          type="submit"
          disabled={submitting || !username || !password}
          className="w-full rounded bg-blue-700 px-4 py-2 text-sm font-semibold text-white hover:bg-blue-800 disabled:opacity-50"
        >
          {submitting ? 'Signing in…' : 'Sign in'}
        </button>

        <div className="rounded-md border border-dashed border-gray-300 bg-gray-50 p-3 text-xs text-gray-600">
          <div className="mb-1 font-medium text-gray-700">Demo staff login</div>
          <div className="font-mono">{DEMO_USERNAME}</div>
          <div className="font-mono">{DEMO_PASSWORD}</div>
          <button
            type="button"
            onClick={() => {
              setUsername(DEMO_USERNAME);
              setPassword(DEMO_PASSWORD);
            }}
            className="mt-2 rounded border border-gray-300 bg-white px-2 py-1 font-medium text-gray-700 hover:bg-gray-100"
          >
            Fill demo credentials
          </button>
        </div>
      </form>
    </div>
  );
}
