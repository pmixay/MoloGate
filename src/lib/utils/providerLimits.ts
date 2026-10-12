// Mologate: the usage limits provider accounts report (utils/provider_limits.py on the server),
// labelled and formatted the same way on the account cards, in the chat's limits panel and on the
// limits ring next to the model selector.

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

// The few characters beside the limits ring while a window runs low.
export const shortWarning = (item: Outlook, t: Translate = plainText) => {
	if (item.level === 'empty') return t('Used up');
	if (item.minutes_left !== null && !item.lasts_until_reset) {
		return t('~{{DURATION}} left', { DURATION: formatDuration(item.minutes_left * 60) });
	}
	return t('{{PERCENT}} left', { PERCENT: formatPercent(100 - item.used_percent) });
};

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

export const stillDismissed = (item: Outlook, dismissed?: DismissedWarning) =>
	!!dismissed &&
	levelRank(item.level) <= levelRank(dismissed.level) &&
	// Readings can move the reset time by seconds; a later window starts the warning over.
	Math.abs((item.resets_at ?? 0) - (dismissed.resets_at ?? 0)) < 300;
