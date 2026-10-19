import { describe, expect, it } from 'vitest';
import { secondsToMs, toolElapsedMs } from './toolElapsed';
import { buildOutputDisplayItems } from '../chat/Messages/structuredOutput';

describe('tool elapsed timing', () => {
	it('converts persisted seconds to milliseconds', () => {
		expect(secondsToMs('123.5')).toBe(123500);
		expect(secondsToMs('')).toBeNull();
		expect(secondsToMs('invalid')).toBeNull();
	});
	it('ticks active calls and freezes at the recorded end', () => {
		expect(toolElapsedMs(100, undefined, undefined, false, 105000)).toBe(5000);
		expect(toolElapsedMs(100, 103, undefined, false, 999000)).toBe(3000);
		expect(toolElapsedMs(100, 103, undefined, true, 999000)).toBe(3000);
	});
	it('does not fabricate historical durations', () => {
		expect(toolElapsedMs(undefined, undefined, undefined, true, 999000)).toBeNull();
		expect(toolElapsedMs(100, undefined, undefined, true, 999000)).toBeNull();
		expect(toolElapsedMs(undefined, undefined, '2', true, 999000)).toBe(2000);
	});
	it('propagates tool timing as string attributes', () => {
		const [item] = buildOutputDisplayItems([
			{ type: 'function_call', call_id: 'call', started_at: 100, ended_at: 103 }
		]);
		if (item.type !== 'detail_single') throw new Error('Expected tool token');
		expect(item.token.attributes.started_at).toBe('100');
		expect(item.token.attributes.ended_at).toBe('103');
	});
});
