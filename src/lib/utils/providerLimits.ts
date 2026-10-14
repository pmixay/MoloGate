// Mologate: the usage limits provider accounts report (utils/provider_limits.py on the server),
// labelled and formatted the same way on the account cards and in the context indicator's panel.

export type LimitWindow = {
	id: string;
	used_percent: number;
	window_minutes?: number | null;
	resets_at?: number | null;
	// Requests or tokens for header limits; dollars when unit is 'usd'.
	limit?: number;
	remaining?: number;
	spent?: number;
	unit?: 'usd';
	// The part of a shared 5-hour or weekly window the rest of the team used.
	team_percent?: number;
	// Where it came from: an OpenRouter key ('key'), or the usage ledger ('ledger': spend estimated
	// from token counts and published prices, with `unpriced` requests of unknown cost).
	source?: string;
	unpriced?: number;
};

// Translates a key with {{NAME}} placeholders; $i18n.t in components that have it.
export type Translate = (key: string, values?: Record<string, string | number>) => string;

export const plainText: Translate = (key, values = {}) =>
	key.replace(/{{(\w+)}}/g, (match, name) => String(values[name] ?? match));

// Codex and Z.ai report a 5-hour (primary) and a weekly (secondary) window.
export const SUBSCRIPTION_WINDOWS = ['primary', 'secondary'];

export const limitLabel = (window: LimitWindow, t: Translate = plainText) => {
	if (window.id === 'credits') return t('Key credit limit');
	if (window.id === 'budget') return t('Monthly budget');
	if (window.id === 'requests') return t('Requests');
	if (window.id === 'tokens') return t('Tokens');
	if (window.id === 'input-tokens') return t('Input tokens');
	if (window.id === 'output-tokens') return t('Output tokens');

	const minutes = window.window_minutes;
	if (minutes === 10080) return t('Weekly limit');
	if (minutes && minutes % 1440 === 0) return t('{{COUNT}}-day limit', { COUNT: minutes / 1440 });
	if (minutes && minutes % 60 === 0) return t('{{COUNT}}-hour limit', { COUNT: minutes / 60 });
	if (minutes) return t('{{COUNT}}-minute limit', { COUNT: minutes });
	return window.id === 'primary' ? t('Short-term limit') : t('Long-term limit');
};

// A label short enough for a compact bar: "5h", "Week", "Budget".
export const shortLabel = (window: LimitWindow, t: Translate = plainText) => {
	if (window.id === 'credits') return t('Credit');
	if (window.id === 'budget') return t('Budget');
	const minutes = window.window_minutes;
	if (minutes === 10080) return t('Week');
	if (minutes && minutes % 1440 === 0) return `${minutes / 1440}d`;
	if (minutes && minutes % 60 === 0) return `${minutes / 60}h`;
	return limitLabel(window, t);
};

const trimNumber = (value: number) =>
	value >= 10 ? String(Math.round(value)) : value.toFixed(1).replace(/\.0$/, '');

export const formatCount = (value: number) => {
	if (value >= 1_000_000) return `${trimNumber(value / 1_000_000)}m`;
	if (value >= 1_000) return `${trimNumber(value / 1_000)}k`;
	return String(Math.round(value ?? 0));
};

export const formatDollars = (value: number) =>
	`$${value >= 100 ? Math.round(value) : value.toFixed(2).replace(/\.00$/, '')}`;

export const formatPercent = (value: number) =>
	value > 0 && value < 1 ? '<1%' : `${Math.round(value)}%`;

// "1d 4h", "2h 15m", "9m".
export const formatDuration = (seconds: number) => {
	const minutes = Math.max(1, Math.round(seconds / 60));
	const days = Math.floor(minutes / 1440);
	const hours = Math.floor((minutes % 1440) / 60);
	const rest = minutes % 60;
	if (days) return hours ? `${days}d ${hours}h` : `${days}d`;
	if (hours) return rest ? `${hours}h ${rest}m` : `${hours}h`;
	return `${rest}m`;
};

// "$74.50 of $100 left", "1.2k of 5k left" or "37% used".
export const amountText = (window: LimitWindow, t: Translate = plainText) => {
	if (window.limit && window.unit === 'usd') {
		return t('{{REMAINING}} of {{LIMIT}} left', {
			REMAINING: formatDollars(window.remaining ?? 0),
			LIMIT: formatDollars(window.limit)
		});
	}
	if (window.limit) {
		return t('{{REMAINING}} of {{LIMIT}} left', {
			REMAINING: formatCount(window.remaining ?? 0),
			LIMIT: formatCount(window.limit)
		});
	}
	return t('{{PERCENT}} used', { PERCENT: formatPercent(window.used_percent) });
};

export const resetText = (window: LimitWindow, t: Translate = plainText, now = Date.now()) => {
	if (!window.resets_at) return '';
	const seconds = window.resets_at - now / 1000;
	return seconds > 0
		? t('resets in {{DURATION}}', { DURATION: formatDuration(seconds) })
		: t('reset since');
};

// The two windows worth showing at a glance: the 5-hour and weekly windows of a subscription,
// else an API key's credit limit and budget, else whatever the provider reported first.
export const mainWindows = (windows: LimitWindow[]) => {
	for (const ids of [SUBSCRIPTION_WINDOWS, ['credits', 'budget']]) {
		const found = ids
			.map((id) => windows.find((window) => window.id === id))
			.filter((window): window is LimitWindow => !!window);
		if (found.length) return found;
	}
	return windows.slice(0, 2);
};

export const limitLevel = (usedPercent: number) =>
	usedPercent >= 90 ? 'critical' : usedPercent >= 75 ? 'warning' : 'normal';

// How much of each long window is left across every account that can serve a model, and roughly
// how many minutes of work that is at the recent pace (`outlook` from GET /models/model/limits).
export type Outlook = {
	id: string;
	window_minutes?: number | null;
	used_percent: number;
	minutes_left: number | null;
	lasts_until_reset: boolean;
	resets_at: number | null;
	level: 'ok' | 'low' | 'critical' | 'empty';
};

export type ModelLimits = {
	windows: LimitWindow[];
	updated_at: number | null;
	account?: { label: string; provider: string; plan: string } | null;
	accounts?: number;
	outlook?: Outlook[];
	allowance?: Allowance | null;
};

export const OUTLOOK_LEVELS: Outlook['level'][] = ['ok', 'low', 'critical', 'empty'];

export const levelRank = (level: Outlook['level']) => OUTLOOK_LEVELS.indexOf(level);

// The windows running low, the worst first.
export const lowWindows = (outlook: Outlook[]) =>
	outlook
		.filter((item) => item.level !== 'ok')
		.sort((a, b) => levelRank(b.level) - levelRank(a.level));

export const outlookName = (item: Outlook, t: Translate = plainText) =>
	limitLabel(
		{ id: item.id, used_percent: item.used_percent, window_minutes: item.window_minutes },
		t
	);

// "5-hour limit: 17% left, about 44m of work at the recent pace. It resets in 1h 58m."
export const warningText = (item: Outlook, t: Translate = plainText, now = Date.now()) => {
	const values = {
		WINDOW: outlookName(item, t),
		PERCENT: formatPercent(100 - item.used_percent),
		DURATION: item.minutes_left !== null ? formatDuration(item.minutes_left * 60) : '',
		RESET: item.resets_at ? formatDuration(item.resets_at - now / 1000) : ''
	};
	if (item.level === 'empty') {
		return values.RESET
			? t('{{WINDOW}} is used up on every account for this model. It resets in {{RESET}}.', values)
			: t('{{WINDOW}} is used up on every account for this model.', values);
	}
	if (item.minutes_left !== null && !item.lasts_until_reset) {
		return values.RESET
			? t(
					'{{WINDOW}}: {{PERCENT}} left, about {{DURATION}} of work at the recent pace. It resets in {{RESET}}.',
					values
				)
			: t('{{WINDOW}}: {{PERCENT}} left, about {{DURATION}} of work at the recent pace.', values);
	}
	return values.RESET
		? t('{{WINDOW}}: only {{PERCENT}} left. It resets in {{RESET}}.', values)
		: t('{{WINDOW}}: only {{PERCENT}} left.', values);
};

// A dismissed warning stays hidden for that window until it gets worse or the window resets.
export type DismissedWarning = { level: Outlook['level']; resets_at: number | null };

export const stillDismissed = (
	item: Pick<Outlook, 'level' | 'resets_at'>,
	dismissed?: DismissedWarning
) =>
	!!dismissed &&
	levelRank(item.level) <= levelRank(dismissed.level) &&
	// Readings can move the reset time by seconds; a later window starts the warning over.
	Math.abs((item.resets_at ?? 0) - (dismissed.resets_at ?? 0)) < 300;

// Mologate: what the member has left of the selected model (`allowance` from GET
// /models/model/limits). Per window: their own account's half still free plus what is left of their
// share of the team pool (credits and overrides included), as far as the accounts serving the model
// have room. It warns from 80% of that and is empty past it. `limit` is the window that runs out
// first, and `then` what replies use after it.
export type RoomPart = { allowed: number; used: number; left: number };

export type ShareWindow = RoomPart & {
	own: RoomPart;
	pool: { capacity: number; share: number; extra: number; used: number; left: number };
	percent: number;
	level: 'ok' | 'low' | 'empty';
	resets_at: number | null;
	minutes_left: number | null;
};

export type Allowance = {
	mode: 'off' | 'observe' | 'enforce';
	tier: 'reserve' | 'pool' | 'credit' | 'worker' | 'empty';
	model: string;
	model_name: string;
	asked_name: string;
	swapped: boolean;
	reason: string;
	credits: number;
	extra_points: number;
	limit: string | null;
	then: 'worker' | 'wait' | 'continues';
	then_name: string | null;
	windows: Record<string, ShareWindow>;
};

export const shareWindowName = (name: string, t: Translate = plainText) =>
	name === '5h'
		? t('{{COUNT}}-hour limit', { COUNT: 5 })
		: name === 'weekly'
			? t('Weekly limit')
			: name === 'budget'
				? t('Monthly budget')
				: name;

// In observe the rules are only recorded: what they say is what would happen once enforced.
export const observing = (allowance: Allowance | null | undefined) => allowance?.mode === 'observe';

export const tierLabel = (allowance: Allowance, t: Translate = plainText) =>
	({
		reserve: t('Your own account'),
		pool: t('Your share of the team pool'),
		credit: t('Your credits'),
		worker: t('Worker models only'),
		empty: t('No room in the pool')
	})[allowance.tier] ?? allowance.tier;

// Each part's left as a share of everything the member may use in the window, so they add up:
// "5% your account + 8% your pool share = 13% left".
export const roomParts = (window: ShareWindow) => {
	const percentOf = (value: number) =>
		window.allowed > 0 ? Math.max(0, (value / window.allowed) * 100) : 0;
	return {
		own: window.own.allowed > 0 ? percentOf(window.own.left) : null,
		pool: percentOf(window.pool.left),
		total: Math.max(0, 100 - window.percent)
	};
};

export const roomFormula = (window: ShareWindow, t: Translate = plainText) => {
	const parts = roomParts(window);
	const values = {
		OWN: formatPercent(parts.own ?? 0),
		POOL: formatPercent(parts.pool),
		TOTAL: formatPercent(parts.total)
	};
	return parts.own === null
		? t('{{TOTAL}} left, all from your share of the pool', values)
		: t('{{OWN}} your account + {{POOL}} your pool share = {{TOTAL}} left', values);
};

export type ShareWarning = ShareWindow & { id: string; name: string };

// The one window that runs out first, when it is low or used up. A worker model goes on past the
// share, so it needs none.
export const allowanceWarnings = (allowance: Allowance | null | undefined): ShareWarning[] => {
	if (!allowance || allowance.mode === 'off' || allowance.then === 'continues' || !allowance.limit)
		return [];
	const window = allowance.windows?.[allowance.limit];
	return window && window.level !== 'ok'
		? [{ ...window, id: `share:${allowance.limit}`, name: allowance.limit }]
		: [];
};

// "GPT-6: 13% left (5% your account + 8% your pool share), about 40m of work at your recent pace.
// After that, replies use GPT Luna. Weekly limit frees up in 2d 3h."
export const allowanceWarningText = (
	item: ShareWarning,
	allowance: Allowance,
	t: Translate = plainText,
	now = Date.now()
) => {
	const parts = roomParts(item);
	const values = {
		MODEL: allowance.asked_name || allowance.model_name,
		PERCENT: formatPercent(parts.total),
		OWN: formatPercent(parts.own ?? 0),
		POOL: formatPercent(parts.pool),
		DURATION: item.minutes_left !== null ? formatDuration(item.minutes_left * 60) : '',
		WORKER: allowance.then_name ?? '',
		WINDOW: shareWindowName(item.name, t),
		RESET: item.resets_at ? formatDuration(item.resets_at - now / 1000) : ''
	};
	const empty = item.level === 'empty';
	const sentences = [];
	if (empty) {
		sentences.push(t('{{MODEL}}: you have used all you can for now.', values));
	} else {
		let head = t('{{MODEL}}: {{PERCENT}} left', values);
		if (parts.own !== null)
			head += ` (${t('{{OWN}} your account + {{POOL}} your pool share', values)})`;
		if (values.DURATION) head += `, ${t('about {{DURATION}} of work at your recent pace', values)}`;
		sentences.push(`${head}.`);
	}
	if (allowance.then === 'worker' && values.WORKER) {
		sentences.push(
			observing(allowance)
				? t('Once limits are enforced, replies would then use {{WORKER}}.', values)
				: empty
					? t('Replies use {{WORKER}}.', values)
					: t('After that, replies use {{WORKER}}.', values)
		);
	}
	if (values.RESET) sentences.push(t('{{WINDOW}} frees up in {{RESET}}.', values));
	if (observing(allowance))
		sentences.push(t('Limits are not enforced yet, so nothing changes for now.'));
	return sentences.join(' ');
};
