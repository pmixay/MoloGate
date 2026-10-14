import { describe, expect, it } from 'vitest';

import { shareWarningText, shareWarnings, tierLabel, type Allowance } from './providerLimits';

// Mologate: the share warning says what happens in enforce, and what would happen in observe.
const allowance = (
	mode: Allowance['mode'],
	tier: Allowance['tier'],
	percent: number
): Allowance => ({
	mode,
	tier,
	model: 'gpt-luna-6',
	model_name: 'GPT Luna',
	swapped: tier === 'worker',
	reason: '',
	credits: 0,
	extra_points: 0,
	windows: {
		weekly: {
			capacity: 150,
			share: 50,
			extra: 0,
			used: (percent / 100) * 50,
			left: 50 - (percent / 100) * 50,
			percent,
			level: percent >= 100 ? 'empty' : percent >= 80 ? 'low' : 'ok',
			resets_at: null,
			minutes_left: null
		}
	}
});

describe('share warnings', () => {
	it('says replies use the worker model when enforced', () => {
		const value = allowance('enforce', 'worker', 100);
		const [item] = shareWarnings(value);
		expect(shareWarningText(item, value)).toBe('Weekly share is used up, so replies use GPT Luna.');
	});

	it('says what would happen when only observed', () => {
		const value = allowance('observe', 'worker', 100);
		const [item] = shareWarnings(value);
		expect(shareWarningText(item, value)).toBe(
			'Weekly share is used up. Once limits are enforced, replies would use GPT Luna. Limits are not enforced yet, so nothing changes for now.'
		);
	});

	it('reports the share at 80% the same way in both modes', () => {
		for (const mode of ['observe', 'enforce'] as const) {
			const value = allowance(mode, 'pool', 84);
			const [item] = shareWarnings(value);
			expect(shareWarningText(item, value)).toBe('Weekly share: 84% used.');
		}
	});

	it('warns nobody on their own account, or with limits off', () => {
		expect(shareWarnings(allowance('enforce', 'reserve', 100))).toEqual([]);
		expect(shareWarnings(allowance('off', 'worker', 100))).toEqual([]);
		expect(tierLabel(allowance('observe', 'worker', 100))).toBe('Worker models only');
	});
});
