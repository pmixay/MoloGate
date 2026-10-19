import { describe, expect, it } from 'vitest';

import { buildContextBreakdown, estimateTokens, getPromptTokens } from './contextBreakdown';

const categories = (tool_activity = 0) => ({
	system: 100,
	tools: 20,
	summary: 30,
	messages: 50,
	tool_activity,
	images: 0
});
const result = (text: string) => ({ type: 'function_call_output', output: text });
const assistant = (output: any[], toolActivity = 0, toolResultTokens?: number) => ({
	role: 'assistant',
	output,
	context_breakdown: {
		categories: categories(toolActivity),
		estimated_tokens: 200 + toolActivity,
		...(toolResultTokens === undefined ? {} : { tool_result_tokens: toolResultTokens })
	}
});

describe('context breakdown', () => {
	it('keeps legacy fallback and four-character estimation', () => {
		expect(buildContextBreakdown([])).toBeNull();
		expect(buildContextBreakdown([{ role: 'user', content: 'hello' }], 123.4)).toMatchObject({
			total: 123,
			recorded: false,
			calibrated: false
		});
		expect(estimateTokens('abcdefghi')).toBe(2);
		expect(estimateTokens('a')).toBe(1);
		expect(estimateTokens(null)).toBe(0);
		expect(getPromptTokens({ prompt_n: 3, cache_n: 7 })).toBe(10);
	});

	it('preserves small-result estimates and handles old records without a result subtotal', () => {
		const breakdown = buildContextBreakdown([
			assistant(
				[
					{ type: 'function_call', name: 'read', arguments: 'abcdefgh' },
					result('abcdefghijkl'),
					{ type: 'message', content: [{ text: 'done' }] }
				],
				40
			)
		]);
		expect(breakdown).toMatchObject({
			categories: { ...categories(50), messages: 55 },
			total: 255,
			recorded: true,
			calibrated: false
		});
	});

	it('caps huge saved results per output and across messages without modifying saved output', () => {
		const huge = 'x'.repeat(1_000_000);
		const first = assistant([result(huge)], 0, 0);
		const later = Array.from({ length: 9 }, () => ({ role: 'assistant', output: [result(huge)] }));
		expect(buildContextBreakdown([first])?.categories.tool_activity).toBe(6004);
		const breakdown = buildContextBreakdown([first, ...later]);
		expect(breakdown?.categories.tool_activity).toBe(24000 + 10 * 4);
		expect(first.output[0].output).toBe(huge);
		expect(later[8].output[0].output.length).toBe(1_000_000);
	});

	it('uses the recorded result subset, not tool arguments, to consume the shared budget', () => {
		const message = assistant(
			[
				{ type: 'function_call', name: 'edit', arguments: 'a'.repeat(160_000) },
				result('x'.repeat(100_000)),
				result('x'.repeat(100_000))
			],
			70000,
			22000
		);
		expect(buildContextBreakdown([message])?.categories.tool_activity).toBe(
			70000 + 40000 + 1 + 2000 + 8
		);
	});

	it('handles exhausted and legacy budgets without removing existing prompt estimates', () => {
		expect(
			buildContextBreakdown([assistant([result('x'.repeat(100_000))], 30000, 25000)])?.categories
				.tool_activity
		).toBe(30004);
		// No reliable result/argument split in older records: bound new results independently.
		expect(
			buildContextBreakdown([assistant([result('x'.repeat(100_000))], 30000)])?.categories
				.tool_activity
		).toBe(36004);
	});

	it('starts from the latest recorded prompt rather than replaying older raw output', () => {
		expect(
			buildContextBreakdown([
				assistant([result('x'.repeat(100_000))]),
				assistant([result('tiny')], 100, 80)
			])?.categories.tool_activity
		).toBe(105);
	});

	it('counts image estimates separately from bounded result text', () => {
		const message = assistant(
			[
				{
					type: 'function_call_output',
					output: [
						{ type: 'text', text: 'x'.repeat(100_000) },
						{ type: 'image_url', image_url: 'saved' }
					]
				}
			],
			0,
			24000
		);
		expect(buildContextBreakdown([message])?.categories).toMatchObject({
			tool_activity: 4,
			images: 1000
		});
	});

	it('retains provider calibration and skips multi-call tool usage', () => {
		const calibrated = { ...assistant([]), content: 'done', usage: { prompt_tokens: 400 } };
		expect(buildContextBreakdown([calibrated])).toMatchObject({
			total: 410,
			calibrated: true,
			categories: { system: 200, tools: 40, summary: 60, messages: 110 }
		});
		const toolTurn = {
			...assistant([{ type: 'function_call', name: 'read', arguments: '' }]),
			usage: { prompt_tokens: 400 }
		};
		expect(buildContextBreakdown([toolTurn])?.calibrated).toBe(false);
		expect(
			buildContextBreakdown([{ ...calibrated, usage: { prompt_tokens: 10000 } }])?.calibrated
		).toBe(false);
	});
});
