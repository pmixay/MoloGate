// Mologate: what fills a chat's context window, for the composer's context indicator.
//
// Each fresh turn's assistant message carries `context_breakdown`: the server's estimate of the
// prompt it sent, split into categories (utils/context_window.py). The window right now holds
// that prompt plus everything said since: the message's own output and any later messages. The
// estimate (four characters per token) is calibrated against the prompt tokens the provider
// reported for the latest turn that made a single model call; with tool calls in the turn, the
// reported usage sums several calls and can't be compared.

export const CONTEXT_CATEGORIES = [
	'system',
	'tools',
	'summary',
	'messages',
	'tool_activity',
	'images'
] as const;

export type ContextCategory = (typeof CONTEXT_CATEGORIES)[number];
export type ContextCategories = Record<ContextCategory, number>;

export type ContextBreakdown = {
	categories: ContextCategories;
	total: number;
	// False when no message in the branch recorded a breakdown (older chats): everything is
	// counted as messages.
	recorded: boolean;
	// True when the estimate was scaled to a provider's reported prompt tokens.
	calibrated: boolean;
};

const IMAGE_TOKENS = 1000;
const MESSAGE_OVERHEAD_TOKENS = 4;
// Model-only text limits in tool_context.py, converted with the four-char token estimate.
const TOOL_RESULT_MAX_TOKENS = 6000;
const TOOL_RESULTS_MAX_TOKENS = 24000;

type ToolResultBudget = { tokens: number };
// A calibration far outside this range means the estimate and the usage describe different things.
const MIN_CALIBRATION = 0.4;
const MAX_CALIBRATION = 2.5;

const emptyCategories = (): ContextCategories =>
	Object.fromEntries(CONTEXT_CATEGORIES.map((category) => [category, 0])) as ContextCategories;

export const estimateTokens = (value: unknown) => {
	if (value === null || value === undefined || value === '') {
		return 0;
	}
	let text: string;
	if (typeof value === 'string') {
		text = value;
	} else {
		try {
			text = JSON.stringify(value);
		} catch {
			text = String(value);
		}
	}
	return text ? Math.max(1, Math.floor(text.length / 4)) : 0;
};

const toCount = (value: unknown) => {
	const parsed = Number(value || 0);
	return Number.isFinite(parsed) && parsed > 0 ? Math.trunc(parsed) : 0;
};

export const getPromptTokens = (usage?: Record<string, unknown> | null) => {
	if (!usage) {
		return 0;
	}
	return (
		toCount(usage.prompt_tokens) ||
		toCount(usage.prompt_eval_count) ||
		toCount(usage.prompt_n) + toCount(usage.cache_n) ||
		toCount(usage.input_tokens)
	);
};

const isImageFile = (file: any) =>
	file?.type === 'image' ||
	(typeof file?.content_type === 'string' &&
		file.content_type.startsWith('image/') &&
		!file.content_type.startsWith('image/svg'));

const outputText = (parts: unknown) =>
	Array.isArray(parts)
		? parts.map((part: any) => (typeof part === 'string' ? part : (part?.text ?? ''))).join('\n')
		: typeof parts === 'string'
			? parts
			: '';

const outputImageTokens = (parts: unknown) =>
	Array.isArray(parts)
		? parts.filter((part: any) => ['image_url', 'input_image'].includes(part?.type)).length *
			IMAGE_TOKENS
		: 0;

const hasToolCalls = (message: any) =>
	Array.isArray(message?.output) &&
	message.output.some((item: any) => item?.type === 'function_call');

// What one message adds to the next prompt.
export const addMessageTokens = (
	categories: ContextCategories,
	message: any,
	toolResults: ToolResultBudget = { tokens: 0 }
) => {
	if (!message) {
		return;
	}

	if (message.role === 'assistant' && Array.isArray(message.output) && message.output.length) {
		for (const item of message.output) {
			if (item?.type === 'message') {
				categories.messages += estimateTokens(outputText(item.content));
				categories.images += outputImageTokens(item.content);
			} else if (item?.type === 'function_call') {
				categories.tool_activity += estimateTokens(item.arguments) + estimateTokens(item.name);
			} else if (item?.type === 'function_call_output') {
				// Saved output is deliberately full fidelity, not the bounded model payload.
				// Approximate its projection without modifying history: unlike the backend's
				// recent-first allocation with older excerpts, only the shared subtotal matters
				// here. Base framing consumes a few text-budget tokens conservatively; new
				// framing remains outside the text cap. Arguments and images are never capped.
				const textTokens = Math.min(
					estimateTokens(outputText(item.output)),
					TOOL_RESULT_MAX_TOKENS,
					Math.max(0, TOOL_RESULTS_MAX_TOKENS - toolResults.tokens)
				);
				toolResults.tokens += textTokens;
				categories.tool_activity += MESSAGE_OVERHEAD_TOKENS + textTokens;
				categories.images += outputImageTokens(item.output);
			}
		}
		categories.messages += MESSAGE_OVERHEAD_TOKENS;
		return;
	}

	categories.messages += MESSAGE_OVERHEAD_TOKENS + estimateTokens(message.content);
	categories.images += (message.files ?? []).filter(isImageFile).length * IMAGE_TOKENS;
};

const getCalibration = (messages: any[]) => {
	for (let idx = messages.length - 1; idx >= 0; idx -= 1) {
		const message = messages[idx];
		const estimated = Number(message?.context_breakdown?.estimated_tokens);
		const reported = getPromptTokens(message?.usage ?? message?.info?.usage);
		if (message?.role !== 'assistant' || !estimated || !reported || hasToolCalls(message)) {
			continue;
		}
		const ratio = reported / estimated;
		if (ratio >= MIN_CALIBRATION && ratio <= MAX_CALIBRATION) {
			return ratio;
		}
	}
	return null;
};

// `messages` is the current branch, oldest first. `fallbackTokens` is used when nothing in the
// branch recorded a breakdown.
export const buildContextBreakdown = (
	messages: any[],
	fallbackTokens = 0
): ContextBreakdown | null => {
	if (!messages?.length) {
		return null;
	}

	let baseIdx = -1;
	for (let idx = messages.length - 1; idx >= 0; idx -= 1) {
		if (messages[idx]?.role === 'assistant' && messages[idx]?.context_breakdown?.categories) {
			baseIdx = idx;
			break;
		}
	}

	if (baseIdx < 0) {
		const categories = emptyCategories();
		categories.messages = Math.max(0, Math.round(fallbackTokens));
		return { categories, total: categories.messages, recorded: false, calibrated: false };
	}

	const categories = emptyCategories();
	const recorded = messages[baseIdx].context_breakdown.categories;
	for (const category of CONTEXT_CATEGORIES) {
		categories[category] = toCount(recorded[category]);
	}
	// This field is a subset of tool_activity, not an extra category. Older records
	// cannot distinguish results from arguments: start their incremental budget at
	// zero rather than guessing that arguments have exhausted it. Subsequent raw
	// outputs are still bounded, while the recorded prompt estimate remains intact.
	const toolResults = { tokens: toCount(messages[baseIdx].context_breakdown.tool_result_tokens) };
	for (const message of messages.slice(baseIdx)) {
		addMessageTokens(categories, message, toolResults);
	}

	const calibration = getCalibration(messages);
	if (calibration) {
		for (const category of CONTEXT_CATEGORIES) {
			categories[category] = Math.round(categories[category] * calibration);
		}
	}

	const total = CONTEXT_CATEGORIES.reduce((sum, category) => sum + categories[category], 0);
	return { categories, total, recorded: true, calibrated: calibration !== null };
};
