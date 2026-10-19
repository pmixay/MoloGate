import { describe, expect, it } from 'vitest';
import { getToolPreview } from './toolPreview';
describe('tool row preview', () => {
	it('shows the command rather than the generic tool name', () => {
		expect(getToolPreview({ command: 'git status --short' })).toBe('git status --short');
	});
	it('uses the first nonempty line', () => {
		expect(getToolPreview({ command: '\n  npm test\necho finished' })).toBe('npm test');
	});
	it('limits long commands', () => {
		expect(getToolPreview({ cmd: 'x'.repeat(200) })).toBe('x'.repeat(99) + '…');
	});
	it('handles other tools', () => {
		expect(getToolPreview({ query: 'latest release' })).toBe('latest release');
		expect(getToolPreview({ path: '/home/user/app.py' })).toBe('/home/user/app.py');
	});
	it('handles incomplete arguments without throwing', () => {
		expect(getToolPreview(null)).toBe('');
		expect(getToolPreview({ command: 123 })).toBe('');
	});
});
