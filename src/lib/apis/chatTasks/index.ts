import { WEBUI_API_BASE_URL } from '$lib/constants';

// Mologate: the work a chat's agent started apart from its reply: sub-agents, monitors and
// scheduled prompts (backend utils/agent_tasks.py). Not the agent's checklist (chat.tasks).
export type TaskType = 'subagent' | 'monitor' | 'scheduled';
export type TaskStatus = 'running' | 'scheduled' | 'completed' | 'failed' | 'cancelled';

export type TaskSchedule = {
	next_run_at: number | null;
	every: number | null;
	runs: number;
	max_runs: number | null;
	ends_at: number | null;
	failures: number;
};

export type TaskMonitor = {
	kind: 'command' | 'http';
	target: string;
	interval: number;
	checks: number;
	max_checks: number;
	ends_at: number | null;
};

// Times are epoch seconds.
export type Task = {
	// The internal chat row's id, used in URLs.
	id: string;
	// deleg_…, mon_… or sched_…, as the agent knows it.
	handle: string;
	type: TaskType;
	title: string;
	status: TaskStatus;
	mode: 'foreground' | 'background' | null;
	created_at: number;
	started_at: number | null;
	finished_at: number | null;
	// null while running
	duration: number | null;
	preview: string | null;
	error: string | null;
	// A sub-agent's own conversation.
	conversation_id: string | null;
	// The main chat's answer that started it.
	message_id: string | null;
	profile: string | null;
	schedule: TaskSchedule | null;
	monitor: TaskMonitor | null;
	can_stop: boolean;
};

export type TaskRun = {
	started_at: number;
	finished_at: number | null;
	status: string;
	message_id: string | null;
	error: string | null;
};

export type TaskCheck = {
	at: number;
	met: boolean;
	exit_code: number | null;
	http_status: number | null;
};

export type TaskDetail = Task & {
	prompt: string | null;
	output: string | null;
	// output is the live stream of a running sub-agent
	live: boolean;
	activity: { name: string; status: string }[];
	runs: TaskRun[];
	checks: TaskCheck[];
};

const request = async <T>(token: string, path: string, method = 'GET'): Promise<T> => {
	const response = await fetch(`${WEBUI_API_BASE_URL}/chats${path}`, {
		method,
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` })
		}
	});
	const data = await response.json().catch(() => null);
	if (!response.ok || data === null) {
		throw new Error(data?.detail ?? `HTTP ${response.status}`);
	}
	return data as T;
};

const taskPath = (chatId: string, taskId = '') =>
	`/${encodeURIComponent(chatId)}/tasks${taskId ? `/${encodeURIComponent(taskId)}` : ''}`;

// Active tasks first, then finished ones; cleared tasks are left out.
export const getChatTasks = (token: string, chatId: string) =>
	request<{ tasks: Task[]; running: number }>(token, taskPath(chatId));

export const getChatTask = (token: string, chatId: string, taskId: string) =>
	request<TaskDetail>(token, taskPath(chatId, taskId));

// Idempotent: a finished task comes back unchanged.
export const stopChatTask = (token: string, chatId: string, taskId: string) =>
	request<{ task: Task }>(token, `${taskPath(chatId, taskId)}/stop`, 'POST');

// Hides the finished tasks from the list.
export const clearChatTasks = (token: string, chatId: string) =>
	request<{ cleared: number }>(token, `${taskPath(chatId)}/clear`, 'POST');
