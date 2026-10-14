import type { Task, TaskStatus, TaskType } from '$lib/apis/chatTasks';

type Translate = (key: string, options?: Record<string, unknown>) => string;

// "45s", "2m 13s", "1h 4m", "2d 3h".
export const formatSeconds = (value: number) => {
	const seconds = Math.max(0, Math.floor(value));
	if (seconds < 60) return `${seconds}s`;
	const minutes = Math.floor(seconds / 60);
	if (minutes < 60) return seconds % 60 ? `${minutes}m ${seconds % 60}s` : `${minutes}m`;
	const hours = Math.floor(minutes / 60);
	if (hours < 24) return minutes % 60 ? `${hours}h ${minutes % 60}m` : `${hours}h`;
	const days = Math.floor(hours / 24);
	return hours % 24 ? `${days}d ${hours % 24}h` : `${days}d`;
};

// A wait, to the minute once it's a minute or more: "30s", "42m", "1h 5m".
export const formatWait = (value: number) =>
	value < 60 ? formatSeconds(Math.ceil(value)) : formatSeconds(Math.ceil(value / 60) * 60);

export const statusLabel = (status: TaskStatus, t: Translate) =>
	({
		running: t('Running'),
		scheduled: t('Scheduled'),
		completed: t('Completed'),
		failed: t('Failed'),
		cancelled: t('Cancelled')
	})[status] ?? status;

export const typeLabel = (type: TaskType, t: Translate) =>
	({
		subagent: t('Sub-agent'),
		monitor: t('Monitor'),
		scheduled: t('Scheduled task')
	})[type] ?? type;

export const runsText = (runs: number, t: Translate) =>
	runs === 1 ? t('1 run') : t('{{COUNT}} runs', { COUNT: runs });

export const checksText = (checks: number, t: Translate) =>
	checks === 1 ? t('1 check') : t('{{COUNT}} checks', { COUNT: checks });

export const taskDuration = (task: Task) =>
	task.duration ??
	(task.started_at && task.finished_at ? Math.max(0, task.finished_at - task.started_at) : null);

// The row's time: how long it has been running, how long it took, or when a schedule runs next.
export const timeText = (task: Task, now: number, t: Translate) => {
	const nowSeconds = now / 1000;
	const parts: string[] = [];
	if (task.status === 'scheduled') {
		const next = task.schedule?.next_run_at;
		if (next) {
			parts.push(
				next > nowSeconds
					? t('next in {{TIME}}', { TIME: formatWait(next - nowSeconds) })
					: t('due now')
			);
		}
	} else if (task.status === 'running') {
		if (task.started_at) parts.push(formatSeconds(nowSeconds - task.started_at));
	} else {
		const duration = taskDuration(task);
		// A schedule that ran several times shows its runs, not how long it lasted.
		const repeated = task.type === 'scheduled' && (task.schedule?.runs ?? 0) > 1;
		if (duration !== null && !repeated) {
			parts.push(t('took {{TIME}}', { TIME: formatSeconds(duration) }));
		}
	}
	if (task.schedule?.runs) parts.push(runsText(task.schedule.runs, t));
	if (task.monitor?.checks) parts.push(checksText(task.monitor.checks, t));
	return parts.join(' · ');
};
