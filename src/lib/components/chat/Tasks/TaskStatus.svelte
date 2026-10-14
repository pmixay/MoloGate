<script lang="ts">
	import { getContext } from 'svelte';
	import type { Readable } from 'svelte/store';

	import type { TaskStatus } from '$lib/apis/chatTasks';
	import { statusLabel } from './format';

	const i18n =
		getContext<Readable<{ t: (key: string, options?: Record<string, unknown>) => string }>>('i18n');

	export let status: TaskStatus;

	const CLASSES: Record<TaskStatus, string> = {
		running: 'bg-lime-50 text-lime-800 dark:bg-accent/10 dark:text-accent',
		scheduled: 'bg-sky-50 text-sky-700 dark:bg-sky-500/10 dark:text-sky-300',
		completed: 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-400',
		failed: 'bg-red-50 text-red-600 dark:bg-red-500/10 dark:text-red-400',
		cancelled: 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400'
	};
</script>

<span
	class="inline-flex shrink-0 items-center gap-1 rounded-full px-1.5 py-px text-[0.625rem] leading-4 {CLASSES[
		status
	] ?? CLASSES.cancelled}"
>
	{#if status === 'running'}
		<span class="relative flex size-1.5 shrink-0" aria-hidden="true">
			<span
				class="absolute inline-flex h-full w-full animate-ping rounded-full bg-lime-400 opacity-75 dark:bg-accent"
			></span>
			<span class="relative inline-flex size-1.5 rounded-full bg-lime-500 dark:bg-accent"></span>
		</span>
	{/if}
	{statusLabel(status, (key, options) => $i18n.t(key, options))}
</span>
