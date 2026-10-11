import { WEBUI_API_BASE_URL } from '$lib/constants';

export type Sharing = 'private' | 'shared' | 'paused';
export type ProviderAccount = {
	id: string;
	owner_user_id: string;
	owner_name: string;
	provider: string;
	plan: string;
	label: string;
	sharing: Sharing;
	budget: { amount: number; currency: string } | null;
	allow_team_resets: boolean;
	status: string;
	last_error: string | null;
	can_manage: boolean;
	models: { id: string; name: string }[];
	limits: {
		updated_at: number;
		windows: { id: string; used_percent: number; resets_at?: number; window_minutes?: number }[];
		reset_credits?: number;
	} | null;
	last_auto_reset_at: number | null;
};

export type AutoReset = {
	enabled: boolean;
	changed_by: string | null;
	changed_at: number | null;
};

export type DeviceLogin = {
	id: string;
	status: 'pending' | 'completed' | 'error';
	user_code?: string;
	verification_url?: string;
	expires_at: number;
	error?: string;
};

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
	const data = await response.json();
	if (!response.ok) {
		throw new Error(typeof data.detail === 'string' ? data.detail : 'The account request failed.');
	}
	return data;
};
