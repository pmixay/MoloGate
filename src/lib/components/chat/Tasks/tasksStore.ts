import { readable, writable } from 'svelte/store';
import type { Socket } from 'socket.io-client';

import { chatId, socket } from '$lib/stores';
import { getChatTasks, type Task, type TaskStatus, type TaskType } from '$lib/apis/chatTasks';
import { isSavedChatId } from '$lib/utils/chatId';

// Mologate: the open chat's tasks, for the Tasks tab, its header button and the message rows that
// link to it. Live while anything subscribes: fetched when the chat changes, again on each
// chat:task nudge from the backend (debounced) and when the socket reconnects.

export type TaskEvent = {
	task_id: string;
	handle: string | null;
	task_type: TaskType;
	status: TaskStatus;
	notify: { title: string; body: string } | null;
};

export type ChatTasksState = {
	chatId: string;
	tasks: Task[];
	running: number;
	loaded: boolean;
	// The chat has had tasks since it was opened: keeps the tab after Clear empties the list.
	visible: boolean;
	// The latest nudge for this chat, so an open task can refetch.
	event: (TaskEvent & { at: number }) | null;
};

export const ACTIVE_STATUSES: TaskStatus[] = ['running', 'scheduled'];
export const isActiveTask = (task: Pick<Task, 'status'>) => ACTIVE_STATUSES.includes(task.status);

const REFRESH_DELAY = 300;

const EMPTY: ChatTasksState = {
	chatId: '',
	tasks: [],
	running: 0,
	loaded: false,
	visible: false,
	event: null
};

let currentChatId = '';
let lastRequest = 0;
let refreshTimer: ReturnType<typeof setTimeout> | null = null;

export const refreshChatTasks = async () => {
	const id = currentChatId;
	if (!isSavedChatId(id)) return;
	const request = ++lastRequest;
	try {
		const res = await getChatTasks(localStorage.token, id);
		if (request !== lastRequest || id !== currentChatId) return;
		const tasks = Array.isArray(res?.tasks) ? res.tasks : [];
		chatTasks.update((state) => ({
			...state,
			tasks,
			running: res?.running ?? tasks.filter((task) => task.status === 'running').length,
			loaded: true,
			visible: state.visible || tasks.length > 0
		}));
	} catch {
		if (request === lastRequest && id === currentChatId) {
			chatTasks.update((state) => ({ ...state, loaded: true }));
		}
	}
};

const scheduleRefresh = () => {
	if (refreshTimer) clearTimeout(refreshTimer);
	refreshTimer = setTimeout(() => {
		refreshTimer = null;
		void refreshChatTasks();
	}, REFRESH_DELAY);
};

// Shows a task the server just returned (e.g. after Stop) before the next refetch.
export const replaceTask = (task: Task) => {
	chatTasks.update((state) => {
		const tasks = state.tasks.map((item) => (item.id === task.id ? task : item));
		return {
			...state,
			tasks,
			running: tasks.filter((item) => item.status === 'running').length
		};
	});
};

export const chatTasks = writable<ChatTasksState>(EMPTY, () => {
	let boundSocket: Socket | null = null;

	const onEvent = (event: any) => {
		const data = event?.data;
		if (data?.type !== 'chat:task' || !currentChatId || event?.chat_id !== currentChatId) return;
		chatTasks.update((state) => ({ ...state, event: { ...data.data, at: Date.now() } }));
		scheduleRefresh();
	};
	const onConnect = () => void refreshChatTasks();

	const unsubscribeChat = chatId.subscribe((id) => {
		if ((id ?? '') === currentChatId) return;
		currentChatId = id ?? '';
		chatTasks.set({ ...EMPTY, chatId: currentChatId });
		void refreshChatTasks();
	});
	const unsubscribeSocket = socket.subscribe((value) => {
		boundSocket?.off('events', onEvent);
		boundSocket?.off('connect', onConnect);
		boundSocket = value;
		boundSocket?.on('events', onEvent);
		boundSocket?.on('connect', onConnect);
	});

	return () => {
		unsubscribeChat();
		unsubscribeSocket();
		boundSocket?.off('events', onEvent);
		boundSocket?.off('connect', onConnect);
		if (refreshTimer) clearTimeout(refreshTimer);
		refreshTimer = null;
		currentChatId = '';
		lastRequest++;
	};
});

// Ticks every second, for the elapsed time of running tasks.
export const now = readable(Date.now(), (set) => {
	const timer = setInterval(() => set(Date.now()), 1000);
	return () => clearInterval(timer);
});

// The task a link names, by row id or handle.
export const findTask = (tasks: Task[], idOrHandle: string | null | undefined) =>
	idOrHandle
		? (tasks.find((task) => task.id === idOrHandle || task.handle === idOrHandle) ?? null)
		: null;
