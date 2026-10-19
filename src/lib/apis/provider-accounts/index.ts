import { WEBUI_API_BASE_URL } from '$lib/constants';
import type { LimitWindow } from '$lib/utils/providerLimits';

// Mologate: every account is shared with the team; its owner can pause it.
export type Sharing = 'shared' | 'paused';
export type ProviderAccount = {
	id: string;
	// Mologate: null for a team account, which belongs to no member.
	owner_user_id: string | null;
	owner_name: string;
	provider: string;
	plan: string;
	label: string;
	sharing: Sharing;
	budget: { amount: number; currency: string } | null;
	status: string;
	last_error: string | null;
	can_manage: boolean;
	models: { id: string; name: string }[];
	limits: {
		updated_at: number;
		windows: LimitWindow[];
		reset_credits?: number;
	} | null;
	last_reset_at: number | null;
	// Mologate: each member's use from the usage ledger, this month (API keys) or this week.
	usage?: {
		period: 'month' | 'week';
		since: number;
		members: {
			user_id: string;
			name: string;
			requests: number;
			input_tokens: number;
			output_tokens: number;
			cost: number | null;
			unpriced: number;
		}[];
	};
};

// Mologate: the member's own metered use (docs/limits-and-accounts.md §8), read-only.
export type MeteredWindow = {
	window: '5h' | 'weekly';
	since: number;
	resets_at: number | null;
	account_used_percent: number | null;
	calibrated: boolean;
	units: number | null;
	percent: number | null;
	outside_units?: number;
	outside_percent?: number;
};

export type MyUsage = {
	since: number;
	units: Record<string, Record<string, number>>;
	accounts: {
		id: string;
		label: string;
		provider: string;
		plan: string;
		owner: boolean;
		requests: number;
		input_tokens: number;
		output_tokens: number;
		cost: number | null;
		windows: MeteredWindow[];
	}[];
};

export type DeviceLogin = {
	id: string;
	status: 'pending' | 'completed' | 'error';
	method?: 'device_code' | 'link';
	account_id?: string | null;
	user_code?: string | null;
	verification_url?: string | null;
	auth_url?: string | null;
	expires_at: number;
	error?: string | null;
};

// Mologate: the status lets callers drop a sign-in the server no longer has (404).
export class ProviderAccountError extends Error {
	status: number;

	constructor(message: string, status: number) {
		super(message);
		this.status = status;
	}
}

// Mologate: credentials are submitted once and never returned or logged by this client.
export const providerAccountRequest = async <T>(
	token: string,
	path = '',
	method = 'GET',
	body?: object
): Promise<T> => {
	const response = await fetch(`${WEBUI_API_BASE_URL}/provider-accounts${path}`, {
		method,
		headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
		...(body ? { body: JSON.stringify(body) } : {})
	});
	if (!response.ok) {
		// A proxy error page is not JSON; fall back to the generic message.
		const data = await response.json().catch(() => ({}));
		throw new ProviderAccountError(
			typeof data.detail === 'string' ? data.detail : 'The account request failed.',
			response.status
		);
	}
	return response.json();
};

// Mologate: extra pool points an admin grants a member or the whole team (phase 4). Revoked
// ones stay listed: the list is the audit log.
export type LimitOverride = {
	id: string;
	user_id: string | null;
	user_name: string;
	granted_by: string;
	granted_by_name: string;
	extra_points: number;
	until: number | null;
	reason: string;
	revoked_at: number | null;
	revoked_by: string | null;
	revoked_by_name: string | null;
	created_at: number;
};

// Mologate: a member's credits (phase 5): 10 x the plan multiplier for each subscription they
// added, 0.5 per dollar spent through their API keys, spent past their share of the pool.
export type MyCredits = {
	balance: number;
	grants: {
		provider: string;
		plan: string;
		amount: number;
		granted_at: number;
		forfeited_at: number | null;
	}[];
	entries: { delta: number; reason: 'grant' | 'spend' | 'forfeit' | 'admin'; ts: number }[];
	multipliers: Record<string, Record<string, number>>;
	per_dollar: number;
};
