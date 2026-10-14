<script lang="ts">
	import { getContext } from 'svelte';
	import type { Readable } from 'svelte/store';

	import type { Task } from '$lib/apis/chatTasks';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import TaskIcon from './TaskIcon.svelte';
	import TaskStatus from './TaskStatus.svelte';
	import { timeText, typeLabel } from './format';
	import { isActiveTask } from './tasksStore';

	const i18n =
		getContext<Readable<{ t: (key: string, options?: Record<string, unknown>) => string }>>('i18n');

	export let task: Task;
	export let now: number;
	export let stopping = false;
	export let onOpen: () => void = () => {};
	export let onStop: () => void = () => {};

	$: t = (key: string, options?: Record<string, unknown>) => $i18n.t(key, options);
	$: title = task.title || typeLabel(task.type, t);
	$: time = timeText(task, now, t);
	$: canStop = task.can_stop && isActiveTask(task);
</script>

<div
	class="task-row group flex w-full min-w-0 items-center rounded-lg transition-colors duration-75 hover:bg-gray-50 dark:hover:bg-white/4"
>
	<button
		type="button"
		class="flex min-w-0 flex-1 items-start gap-2.5 rounded-lg px-2 py-2 text-left outline-hidden focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-gray-900 dark:focus-visible:ring-accent"
		on:click={onOpen}
	>
		<span class="mt-px shrink-0 text-gray-400 dark:text-gray-500" title={typeLabel(task.type, t)}>
			<TaskIcon type={task.type} className="size-4" />
		</span>
		<span class="min-w-0 flex-1">
			<span class="sr-only">{typeLabel(task.type, t)}:</span>
			<span class="block truncate text-xs text-gray-800 dark:text-gray-200">{title}</span>
			<span
				class="mt-1 flex min-w-0 items-center gap-1.5 text-[0.6875rem] text-gray-400 dark:text-gray-500"
			>
				<TaskStatus status={task.status} />
				{#if time}
					<span class="min-w-0 truncate tabular-nums">{time}</span>
				{/if}
			</span>
		</span>
	</button>
	{#if canStop}
		<button
			type="button"
			class="mr-2 flex h-6 shrink-0 items-center gap-1 rounded-md border border-gray-100 px-2 text-[0.6875rem] text-gray-600 transition hover:bg-white hover:text-gray-900 outline-hidden focus-visible:ring-2 focus-visible:ring-gray-900 disabled:opacity-50 dark:border-white/[0.06] dark:focus-visible:ring-accent dark:text-gray-300 dark:hover:bg-gray-850 dark:hover:text-white"
			disabled={stopping}
			on:click={onStop}
			aria-label={$i18n.t('Stop {{NAME}}', { NAME: title })}
		>
			{#if stopping}
				<Spinner className="size-3" />
			{/if}
			{$i18n.t('Stop')}
		</button>
	{/if}
</div>
