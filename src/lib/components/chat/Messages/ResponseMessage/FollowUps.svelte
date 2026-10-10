<script lang="ts">
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import { onMount, tick, getContext } from 'svelte';

	const i18n = getContext('i18n');

	export let followUps: string[] = [];
	export let onClick: (followUp: string) => void = () => {};
</script>

<div class="mt-4">
	<div
		class="font-mono text-[10.5px] font-medium uppercase tracking-[0.08em] text-gray-500 select-none"
	>
		{$i18n.t('Follow up')}
	</div>

	<div class="flex flex-col text-left mt-2 border-t border-gray-100 dark:border-gray-800">
		{#each followUps as followUp, idx (idx)}
			<Tooltip content={followUp} placement="top-start" className="line-clamp-1">
				<button
					class="group/followup py-2 bg-transparent text-left text-[13.5px] flex items-center gap-2.5 text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-100 transition cursor-pointer w-full"
					on:click={() => onClick(followUp)}
					aria-label={$i18n.t('Follow up: {{question}}', { question: followUp })}
				>
					<svg
						class="size-3.5 shrink-0 text-gray-400 dark:text-gray-600 group-hover/followup:text-gray-600 dark:group-hover/followup:text-gray-400 transition"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="1.8"
						stroke-linecap="round"
						stroke-linejoin="round"
						aria-hidden="true"
					>
						<path d="M5 5v6a3 3 0 0 0 3 3h11" />
						<path d="m15 10 4 4-4 4" />
					</svg>
					<div class="line-clamp-1">
						{followUp}
					</div>
				</button>
			</Tooltip>

			{#if idx < followUps.length - 1}
				<hr class="border-gray-100 dark:border-gray-800" />
			{/if}
		{/each}
	</div>
</div>
