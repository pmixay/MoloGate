import { describe, expect, it } from 'vitest';

import {
	allowanceWarningText,
	allowanceWarnings,
	roomFormula,
	tierLabel,
	type Allowance
} from './providerLimits';

// Mologate: the warning counts the member's own account and their pool share together, for the
// window that runs out first, and says what replies use after it.
const allowance = (
	mode: Allowance['mode'],
	tier: Allowance['tier'],
	{
		own = 0,
		ownLeft = 0,
		poolLeft = 50,
		then = 'worker' as Allowance['then'],
		minutes = null as number | null
	} = {}
): Allowance => {
	const allowed = own + 50;
	const left = ownLeft + poolLeft;
	const percent = Math.round((1 - left / allowed) * 1000) / 10;
	return {
		mode,
		tier,
		model: tier === 'worker' ? 'gpt-luna-6' : 'gpt-6',
		model_name: tier === 'worker' ? 'GPT Luna' : 'GPT-6',
		asked_name: 'GPT-6',
		swapped: tier === 'worker',
		reason: '',
		credits: 0,
		extra_points: 0,
		limit: 'weekly',
		then,
		then_name: then === 'wait' ? null : 'GPT Luna',
		windows: {
			weekly: {
				allowed,
				used: allowed - left,
				left,
				own: { allowed: own, used: own - ownLeft, left: ownLeft },
				pool: { capacity: 150, share: 50, extra: 0, used: 50 - poolLeft, left: poolLeft },
				percent,
				level: left <= 0 ? 'empty' : percent >= 80 ? 'low' : 'ok',
				resets_at: null,
				minutes_left: minutes
			}
		}
	};
};

describe('allowance warnings', () => {
	it('stays quiet while the own account still has room, though the pool share is low', () => {
		const value = allowance('enforce', 'reserve', { own: 50, ownLeft: 40, poolLeft: 5 });
		expect(allowanceWarnings(value)).toEqual([]);
		expect(roomFormula(value.windows.weekly)).toBe(
			'40% your account + 5% your pool share = 45% left'
		);
	});

	it('adds both up when the total runs low, with the pace and what comes next', () => {
		const value = allowance('enforce', 'reserve', {
			own: 50,
			ownLeft: 5,
			poolLeft: 8,
			minutes: 40
		});
		const [item] = allowanceWarnings(value);
		expect(allowanceWarningText(item, value)).toBe(
			'GPT-6: 13% left (5% your account + 8% your pool share), about 40m of work at your recent pace. After that, replies use GPT Luna.'
		);
	});

	it('says replies use the worker model once all of it is used, when enforced', () => {
		const value = allowance('enforce', 'worker', { poolLeft: 0 });
		const [item] = allowanceWarnings(value);
		expect(allowanceWarningText(item, value)).toBe(
			'GPT-6: you have used all you can for now. Replies use GPT Luna.'
		);
	});

	it('says what would happen when only observed', () => {
		const value = allowance('observe', 'worker', { poolLeft: 0 });
		const [item] = allowanceWarnings(value);
		expect(allowanceWarningText(item, value)).toBe(
			'GPT-6: you have used all you can for now. Once limits are enforced, replies would then use GPT Luna. Limits are not enforced yet, so nothing changes for now.'
		);
	});

	it('warns nobody about a worker model, or with limits off', () => {
		expect(
			allowanceWarnings(allowance('enforce', 'worker', { poolLeft: 0, then: 'continues' }))
		).toEqual([]);
		expect(allowanceWarnings(allowance('off', 'worker', { poolLeft: 0 }))).toEqual([]);
		expect(tierLabel(allowance('observe', 'worker'))).toBe('Worker models only');
	});
});
