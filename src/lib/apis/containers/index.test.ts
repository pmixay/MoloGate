import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/constants', () => ({ WEBUI_API_BASE_URL: 'https://app.test/api/v1' }));

import { connectChatGitHub, disconnectChatGitHub, getChatGitHub, pollChatGitHub } from './index';

afterEach(() => vi.unstubAllGlobals());

describe('chat GitHub API', () => {
	it.each([
		[getChatGitHub, 'GET', ''],
		[connectChatGitHub, 'POST', '/connect'],
		[pollChatGitHub, 'POST', '/poll'],
		[disconnectChatGitHub, 'DELETE', '']
	] as const)('uses the saved-chat endpoint for %s', async (api, method, suffix) => {
		const response = { connected: false, pending: true, code: 'ABCD-EFGH' };
		const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => response });
		vi.stubGlobal('fetch', fetch);
		const controller = new AbortController();
		expect(await api('session-token', 'chat/id', controller.signal)).toEqual(response);
		expect(fetch).toHaveBeenCalledWith(
			`https://app.test/api/v1/containers/chats/chat%2Fid/github${suffix}`,
			{
				method,
				signal: controller.signal,
				headers: { Authorization: 'Bearer session-token', 'Content-Type': 'application/json' }
			}
		);
	});

	it('accepts a pending connection before the device code is ready', async () => {
		const response = { connected: false, pending: true };
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => response }));
		expect(await connectChatGitHub('session-token', 'chat')).toEqual(response);
	});

	it('surfaces network-off and authorization errors for toast and retry UI', async () => {
		vi.stubGlobal(
			'fetch',
			vi.fn().mockResolvedValue({
				ok: false,
				status: 409,
				json: async () => ({ detail: 'Enable network access first' })
			})
		);
		await expect(connectChatGitHub('session-token', 'chat')).rejects.toThrow(
			'Enable network access first'
		);
	});

	it('provides a useful error for non-JSON server failures', async () => {
		vi.stubGlobal(
			'fetch',
			vi.fn().mockResolvedValue({
				ok: false,
				status: 503,
				json: async () => {
					throw new Error('not JSON');
				}
			})
		);
		await expect(pollChatGitHub('session-token', 'chat')).rejects.toThrow('HTTP 503');
	});

	it('propagates cancellation without converting it to success', async () => {
		const abort = new DOMException('Aborted', 'AbortError');
		vi.stubGlobal('fetch', vi.fn().mockRejectedValue(abort));
		await expect(getChatGitHub('session-token', 'chat', new AbortController().signal)).rejects.toBe(
			abort
		);
	});
});
