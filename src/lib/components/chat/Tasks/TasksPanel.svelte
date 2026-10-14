<script lang="ts">
	// Mologate: the side panel's Tasks tab. One list of the chat's sub-agents, monitors and
	// scheduled tasks, running and finished, whether or not the agent waits for them.
	import { getContext } from 'svelte';
	import type { Readable } from 'svelte/store';
	import { toast } from 'svelte-sonner';

	import { user } from '$lib/stores';
	import { clearChatTasks, stopChatTask, type Task } from '$lib/apis/chatTasks';

	import Spinner from '$lib/components/common/Spinner.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import TaskDetail from './TaskDetail.svelte';
	import TaskRow from './TaskRow.svelte';
	import {
		chatTasks,
		findTask,
		isActiveTask,
		now,
		refreshChatTasks,
		replaceTask
	} from './tasksStore';

	const i18n =
		getContext<Readable<{ t: (key: string, options?: Record<string, unknown>) => string }>>('i18n');

	export let chatId: string;
	export let history: any = null;
	// The chat's owner when it isn't the viewer's own chat.
	export let chatUser: { id: string } | null = null;
	export let showMessage: Function | null = null;
	// The open task, by row id or handle (a link may only know the handle).
	export let selectedTaskId: string | null = null;

	let clearing = false;
	let stopping: Record<string, boolean> = {};

	$: tasks = $chatTasks.chatId === chatId ? $chatTasks.tasks : [];
	$: loaded = $chatTasks.chatId === chatId && $chatTasks.loaded;
	$: selectedTask = findTask(tasks, selectedTaskId);
	// A task that is gone from the list (cleared, or a stale link) closes.
	const closeMissing = (listLoaded: boolean, task: Task | null) => {
		if (listLoaded && selectedTaskId && !task) selectedTaskId = null;
	};
	$: closeMissing(loaded, selectedTask);
	$: running = tasks.filter((task) => task.status === 'running').length;
	$: scheduled = tasks.filter((task) => task.status === 'scheduled').length;
	$: finished = tasks.filter((task) => !isActiveTask(task)).length;
	$: canClear = !chatUser || chatUser.id === $user?.id || $user?.role === 'admin';
	$: summary = [
		running ? $i18n.t('{{COUNT}} running', { COUNT: running }) : '',
		scheduled ? $i18n.t('{{COUNT}} scheduled', { COUNT: scheduled }) : '',
		finished ? $i18n.t('{{COUNT}} finished', { COUNT: finished }) : ''
	]
		.filter(Boolean)
		.join(' · ');

	const stop = async (task: Task) => {
		stopping = { ...stopping, [task.id]: true };
		try {
			const res = await stopChatTask(localStorage.token, chatId, task.id);
			if (res?.task) replaceTask(res.task);
			void refreshChatTasks();
		} catch (error) {
			toast.error((error as Error)?.message ?? `${error}`);
		} finally {
			const { [task.id]: _, ...rest } = stopping;
			stopping = rest;
		}
	};

	const clear = async () => {
		clearing = true;
		try {
			await clearChatTasks(localStorage.token, chatId);
			await refreshChatTasks();
		} catch (error) {
			toast.error((error as Error)?.message ?? `${error}`);
		} finally {
			clearing = false;
		}
	};
</script>

<div class="tasks-panel flex h-full min-h-0 flex-col">
	{#if selectedTask}
		<TaskDetail
			{chatId}
			task={selectedTask}
			{history}
			{showMessage}
			stopping={!!stopping[selectedTask.id]}
			onStop={() => selectedTask && stop(selectedTask)}
			onBack={() => (selectedTaskId = null)}
		/>
	{:else}
		<div
			class="flex h-8 shrink-0 items-center gap-2 border-b border-gray-50 px-3 dark:border-gray-850/30"
		>
			<div class="min-w-0 flex-1 truncate text-xs text-gray-500 dark:text-gray-400">
				{summary}
			</div>
			{#if canClear}
				<Tooltip content={$i18n.t('Hide the finished tasks')}>
					<button
						type="button"
						class="flex h-6 shrink-0 items-center gap-1 rounded-md px-2 text-xs text-gray-500 transition hover:bg-gray-50 hover:text-gray-900 outline-hidden focus-visible:ring-2 focus-visible:ring-gray-900 disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-gray-500 dark:text-gray-400 dark:hover:bg-white/5 dark:hover:text-gray-100 dark:focus-visible:ring-accent dark:disabled:hover:text-gray-400"
						disabled={finished === 0 || clearing}
						on:click={clear}
						aria-label={$i18n.t('Clear finished tasks')}
					>
						{#if clearing}
							<Spinner className="size-3" />
						{/if}
						{$i18n.t('Clear')}
					</button>
				</Tooltip>
			{/if}
		</div>

		<div class="min-h-0 flex-1 overflow-y-auto scrollbar-hidden px-1.5 py-1">
			{#if !loaded}
				<div class="flex justify-center pt-8"><Spinner className="size-4" /></div>
			{:else if tasks.length === 0}
				<div class="flex flex-col items-center gap-1 px-6 py-12 text-center">
					<div class="text-xs text-gray-500 dark:text-gray-400">{$i18n.t('No tasks')}</div>
					<div class="text-[0.6875rem] text-gray-400 dark:text-gray-600">
						{$i18n.t(
							'Sub-agents, monitors and scheduled tasks the agent starts in this chat show up here.'
						)}
					</div>
				</div>
			{:else}
				<ul class="flex flex-col gap-px" aria-label={$i18n.t('Tasks')}>
					{#each tasks as task (task.id)}
						<li>
							<TaskRow
								{task}
								now={$now}
								stopping={!!stopping[task.id]}
								onOpen={() => (selectedTaskId = task.id)}
								onStop={() => stop(task)}
							/>
						</li>
					{/each}
				</ul>
			{/if}
		</div>
	{/if}
</div>
