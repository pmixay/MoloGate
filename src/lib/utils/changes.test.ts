import { describe, expect, it } from 'vitest';
import { chatChanges } from './changes';

describe('file-change summary updates', () => {
	it('does not count multiple live snapshots as separate file edits', () => {
		const history = {
			currentId: 'm',
			messages: {
				m: {
					changes: { total: 1, files: [{ path: 'a.py', status: 'modified', added: 2, removed: 1 }] }
				}
			}
		};
		expect(chatChanges(history)?.total).toBe(1);
		history.messages.m.changes.files[0].added = 3;
		expect(chatChanges(history)?.added).toBe(3);
	});
	it('clears a reverted live response instead of retaining stale counts', () => {
		expect(
			chatChanges({ currentId: 'm', messages: { m: { changes: { total: 0, files: [] } } } })
		).toBeNull();
	});
});
