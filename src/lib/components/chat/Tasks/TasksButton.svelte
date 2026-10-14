<script lang="ts">
	// Mologate: the header button for the side panel's Tasks tab, once the chat has tasks.
	import { getContext } from 'svelte';
	import type { Readable } from 'svelte/store';

	import { chatControlsTab, showControls, showTasks } from '$lib/stores';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import QueueList from '$lib/components/icons/QueueList.svelte';
	import { chatTasks } from './tasksStore';

	const i18n =
		getContext<Readable<{ t: (key: string, options?: Record<string, unknown>) => string }>>('i18n');

	export let chatId: string;

	$: shown = $chatTasks.chatId === chatId && $chatTasks.visible;
	$: running = shown ? $chatTasks.running : 0;
	$: open = $showControls && $chatControlsTab === 'tasks';
	$: label = running ? $i18n.t('Tasks, {{COUNT}} running', { COUNT: running }) : $i18n.t('Tasks');
</script>

{#if shown}
	<Tooltip content={open ? $i18n.t('Hide tasks') : label}>
		<button
			id="chat-tasks-button"
			class="relative flex size-9 cursor-pointer items-center justify-center rounded-lg transition outline-hidden focus-visible:ring-2 focus-visible:ring-gray-900 dark:focus-visible:ring-accent {open
				? 'bg-gray-100 text-gray-900 dark:bg-gray-850 dark:text-gray-100'
				: 'text-gray-500 hover:bg-gray-100 hover:text-gray-900 dark:text-gray-400 dark:hover:bg-gray-850 dark:hover:text-gray-100'}"
			on:click={() => (open ? showControls.set(false) : showTasks.set({}))}
			aria-label={label}
			aria-pressed={open}
		>
			<QueueList className="size-[18px]" strokeWidth="1.5" />
			{#if running}
				<span
					class="absolute top-1 right-1 flex h-3.5 min-w-3.5 items-center justify-center rounded-full bg-gray-900 px-1 text-[0.5625rem] font-medium leading-none tabular-nums text-white dark:bg-accent dark:text-accent-ink"
					aria-hidden="true"
				>
					{running}
				</span>
			{/if}
		</button>
	</Tooltip>
{/if}
