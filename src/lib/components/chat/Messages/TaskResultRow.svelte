<script lang="ts">
	// Mologate: a background task's result, posted into the chat for the agent (meta.type 'task',
	// backend utils/subagents.py post_task_result). Several results that arrive together are one.
	import { getContext } from 'svelte';
	import type { Readable } from 'svelte/store';

	import { showTasks } from '$lib/stores';
	import ChevronDown from '$lib/components/icons/ChevronDown.svelte';
	import { chatTasks, findTask } from '../Tasks/tasksStore';

	type TaskResult = {
		task_type?: string;
		task_id?: string;
		handle?: string;
		status?: string;
		task_ids?: string[];
		handles?: string[];
	};

	const i18n =
		getContext<Readable<{ t: (key: string, options?: Record<string, unknown>) => string }>>('i18n');

	export let content: string;
	export let result: TaskResult;
	export let chatId = '';

	let expanded = false;

	$: taskIds = Array.isArray(result.task_ids) ? result.task_ids : [];
	$: handles = Array.isArray(result.handles) ? result.handles : [];
	$: count = Math.max(taskIds.length, handles.length, 1);
	$: taskId = result.task_id ?? (count === 1 ? (taskIds[0] ?? handles[0]) : undefined);
	$: handleLabel = count > 1 ? '' : (result.handle ?? handles[0] ?? '');
	$: label =
		count > 1
			? $i18n.t('{{COUNT}} tasks finished', { COUNT: count })
			: result.task_type === 'monitor'
				? result.status === 'completed'
					? $i18n.t('Monitor condition met')
					: result.status === 'cancelled'
						? $i18n.t('Monitor stopped')
						: $i18n.t('Monitor gave up')
				: result.task_type === 'subagent'
					? result.status === 'failed'
						? $i18n.t('Background sub-agent failed')
						: $i18n.t('Background sub-agent finished')
					: result.status === 'failed'
						? $i18n.t('Task failed')
						: $i18n.t('Task finished');
	$: summary = (() => {
		const line = content
			.split('\n')
			.map((value) => value.trim())
			.find((value) => value && !value.startsWith('['));
		if (!line) return '';
		return line.length > 96 ? `${line.slice(0, 96)}...` : line;
	})();
	// The link shows while the panel still lists the task (it isn't cleared).
	$: listed = $chatTasks.chatId === chatId && $chatTasks.visible;
	$: linkable = listed && (count > 1 || !!findTask($chatTasks.tasks, taskId ?? result.handle));
</script>

<div class="w-full min-w-0 pb-1 task-result-row">
	<div class="w-full min-w-0 flex items-center gap-2">
		<button
			type="button"
			class="min-w-0 flex items-center gap-2 text-left text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 transition-colors"
			aria-expanded={expanded}
			on:click={() => (expanded = !expanded)}
		>
			<span class="text-[0.75rem] font-normal shrink-0">{label}</span>
			{#if summary}
				<span class="text-[0.75rem] truncate min-w-0">{summary}</span>
			{/if}
			{#if handleLabel}
				<span
					class="hidden sm:inline text-[0.6875rem] font-mono text-gray-400 dark:text-gray-600 shrink-0"
				>
					{handleLabel}
				</span>
			{/if}
			<ChevronDown
				className="size-3 text-gray-400 dark:text-gray-600 shrink-0 transition-transform duration-150 {expanded
					? 'rotate-180'
					: ''}"
			/>
		</button>
		{#if linkable}
			<button
				type="button"
				class="ml-auto shrink-0 text-[0.6875rem] text-gray-400 dark:text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 hover:underline transition-colors"
				on:click={() => showTasks.set({ taskId: count > 1 ? null : (taskId ?? result.handle) })}
			>
				{$i18n.t('Show in Tasks')}
			</button>
		{/if}
	</div>
	{#if expanded}
		<div
			class="mt-2 ml-3 border-l border-gray-100 dark:border-white/8 pl-3 text-[0.78125rem] leading-relaxed text-gray-600 dark:text-gray-400 whitespace-pre-wrap break-words"
		>
			{content}
		</div>
	{/if}
</div>
