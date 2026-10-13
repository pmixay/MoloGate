import { WEBUI_API_BASE_URL } from '$lib/constants';

// Mologate: each chat's own container (docs/container-nodes.md).
export const CONTAINER_TERMINAL_ID = 'mologate';

export type ChatContainer = {
	exists: boolean;
	network: boolean;
	// none | created | running | paused | exited | unreachable
	state: string;
	node: { id: string; name: string } | null;
	nodes: number;
	limits?: { cpus: number; memory: number; pids: number; disk: number; bandwidth: string } | null;
	disk_used?: number | null;
	files?: { id: string; path: string; name: string; size: number; at: number }[];
};

export type ContainerNode = {
	id: string;
	name: string;
	url: string;
	enabled: boolean;
	key?: string;
	score?: number | null;
	status?: {
		active: number;
		max_active: number;
		cpus: number;
		load1: number;
		memory_total: number;
		memory_available: number;
		disk_free: number;
		accepting: boolean;
		containers: { running: number; paused: number; stopped: number; spare: number };
		features: Record<string, boolean | null>;
	} | null;
};

const request = async <T>(
	token: string,
	path: string,
	method = 'GET',
	body?: object
): Promise<T> => {
	const response = await fetch(`${WEBUI_API_BASE_URL}/containers${path}`, {
		method,
		headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
		...(body ? { body: JSON.stringify(body) } : {})
	});
	const data = await response.json().catch(() => ({}));
	if (!response.ok) {
		throw new Error(data?.detail ?? `HTTP ${response.status}`);
	}
	return data as T;
};

export const getChatContainer = (token: string, chatId: string) =>
	request<ChatContainer>(token, `/chats/${encodeURIComponent(chatId)}`);

export const setChatContainerNetwork = (token: string, chatId: string, enabled: boolean) =>
	request<ChatContainer>(token, `/chats/${encodeURIComponent(chatId)}/network`, 'POST', {
		enabled
	});

// The user started typing: wake the chat's container, or ready one for a new chat.
export const warmUpContainer = (token: string, chatId?: string | null) =>
	request<{ ok: boolean }>(token, '/warmup', 'POST', { chat_id: chatId || null }).catch(() => null);

export const getContainerNodes = (token: string) =>
	request<{ enable: boolean; nodes: ContainerNode[] }>(token, '/nodes');

export const setContainerNodes = (token: string, enable: boolean, nodes: ContainerNode[]) =>
	request<{ enable: boolean; nodes: ContainerNode[] }>(token, '/nodes', 'POST', { enable, nodes });
