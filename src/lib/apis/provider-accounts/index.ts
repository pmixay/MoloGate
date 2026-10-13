import { WEBUI_API_BASE_URL } from '$lib/constants';
import type { LimitWindow } from '$lib/utils/providerLimits';

export type Sharing = 'private' | 'shared' | 'paused';
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
	last_auto_reset_at: number | null;
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

export type AutoReset = {
	enabled: boolean;
	changed_by: string | null;
	changed_at: number | null;
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
