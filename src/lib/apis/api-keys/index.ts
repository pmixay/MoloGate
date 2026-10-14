import { WEBUI_API_BASE_URL } from '$lib/constants';

// Mologate: a member's personal API keys (docs/limits-and-accounts.md §11). A key is returned once,
// when it is made; afterwards only its name and a short hint.
export type KeyLimits = {
	rpm?: number;
	concurrency?: number;
	models?: string[];
	efforts?: string[];
	daily_points?: number;
};

export type KeyUse = {
	requests: number;
	input_tokens: number;
	output_tokens: number;
	cost: number | null;
} | null;

export type PersonalKey = {
	id: string;
	name: string;
	hint: string | null;
	limits: KeyLimits;
	legacy: boolean;
	created_at: number;
	last_used_at: number | null;
	expires_at: number | null;
	revoked_at: number | null;
	usage?: { today: KeyUse; week: KeyUse; points_today: Record<string, number> };
};

export type KeyBounds = {
	concurrency: number;
	rpm: number;
	efforts: string[];
	endpoints: string[];
};

export const apiKeysRequest = async <T>(
	token: string,
	path = '',
	method = 'GET',
	body?: object
): Promise<T> => {
	const response = await fetch(`${WEBUI_API_BASE_URL}/api-keys${path}`, {
		method,
		headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
		...(body ? { body: JSON.stringify(body) } : {})
	});
	if (!response.ok) {
		const data = await response.json().catch(() => ({}));
		const detail = typeof data.detail === 'string' ? data.detail : 'The key request failed.';
		throw new Error(detail);
	}
	return response.json();
};
