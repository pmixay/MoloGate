import { describe, it, expect } from 'vitest';
import { buildContextBreakdown } from './contextBreakdown';

describe('request context after tool clearing', () => {
	it('does not re-add preserved tool history already included in the latest request', () => {
		const message = {
			role: 'assistant',
			context_breakdown: {
				categories: { system: 0, tools: 0, summary: 0, messages: 10, tool_activity: 20, images: 0 },
				estimated_tokens: 30,
				output_items: 2
			},
			output: [
				{ type: 'function_call', name: 'run_command', arguments: 'x'.repeat(10000) },
				{ type: 'function_call_output', output: [{ text: 'y'.repeat(10000) }] }
			]
		};
		expect(buildContextBreakdown([message])?.categories.tool_activity).toBe(20);
	});
	it('still adds results generated after the last request checkpoint', () => {
		const message = {
			role: 'assistant',
			context_breakdown: { categories: { tool_activity: 20 }, output_items: 0 },
			output: [{ type: 'function_call_output', output: [{ text: 'y'.repeat(40) }] }]
		};
		expect(buildContextBreakdown([message])?.categories.tool_activity).toBe(34);
	});
});
