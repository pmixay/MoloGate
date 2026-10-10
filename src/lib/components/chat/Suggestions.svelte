<script lang="ts">
	import Fuse from 'fuse.js';
	import Bolt from '$lib/components/icons/Bolt.svelte';
	import { getContext } from 'svelte';
	import { settings, WEBUI_NAME } from '$lib/stores';
	import { WEBUI_VERSION } from '$lib/constants';

	const i18n = getContext('i18n');

	export let suggestionPrompts = [];
	export let className = '';
	export let inputValue = '';
	export let onSelect = () => {};

	let sortedPrompts = [];

	const fuseOptions = {
		keys: ['content', 'title'],
		threshold: 0.5
	};

	let fuse;
	let filteredPrompts = [];

	// Initialize Fuse
	$: fuse = new Fuse(sortedPrompts, fuseOptions);

	// Update the filteredPrompts if inputValue changes
	// Only increase version if something wirklich geändert hat
	$: if (fuse) getFilteredPrompts(inputValue);

	// Compare objects so translated text refreshes even when IDs stay the same.
	function arraysEqual(a, b) {
		if (a.length !== b.length) return false;
		for (let i = 0; i < a.length; i++) {
			if (a[i] !== b[i]) {
				return false;
			}
		}
		return true;
	}

	const getFilteredPrompts = (inputValue) => {
		if (inputValue.length > 500) {
			filteredPrompts = [];
		} else {
			const newFilteredPrompts =
				inputValue.trim() && fuse
					? fuse.search(inputValue.trim()).map((result) => result.item)
					: sortedPrompts;

			// Compare with the oldFilteredPrompts
			// If there's a difference, update array + version
			if (!arraysEqual(filteredPrompts, newFilteredPrompts)) {
				filteredPrompts = newFilteredPrompts;
			}
		}
	};

	$: if (suggestionPrompts) {
		sortedPrompts = [...(suggestionPrompts ?? [])].sort(() => Math.random() - 0.5);
	}
</script>

<div
	class="mb-2 flex gap-1.5 px-0.5 font-mono text-[10.5px] font-medium uppercase tracking-[0.08em] items-center text-gray-500"
>
	{#if filteredPrompts.length > 0}
		<Bolt className="size-3" />
		{$i18n.t('Suggested')}
	{:else}
		<!-- Keine Vorschläge -->

		<div
			class="flex w-full {$settings?.landingPageMode === 'chat'
				? ' -mt-1'
				: 'text-center items-center justify-center'} self-start normal-case tracking-normal text-[11px] font-normal text-gray-500"
		>
			<!-- LICENSE covers this Open WebUI footer identifier.
			Do not alter, remove, obscure, or replace it except as LICENSE permits:
			https://docs.openwebui.com/license. -->
			{$WEBUI_NAME} ‧ v{WEBUI_VERSION}
		</div>
	{/if}
</div>

<div class="h-36 w-full">
	{#if filteredPrompts.length > 0}
		<div
			role="list"
			class="grid max-h-36 grid-cols-1 gap-1.5 overflow-auto scrollbar-none items-start p-0.5 @xl:grid-cols-3 {className}"
		>
			{#each filteredPrompts as prompt, idx (prompt.id || `${prompt.content}-${idx}`)}
				<!-- svelte-ignore a11y-no-interactive-element-to-noninteractive-role -->
				<button
					role="listitem"
					class="waterfall flex flex-col flex-1 shrink-0 w-full justify-between
				       px-3 py-2.5 rounded-[10px] border border-gray-200 bg-transparent transition-colors
				       hover:bg-gray-50 hover:border-gray-300 dark:border-gray-800 dark:hover:bg-gray-850 dark:hover:border-gray-700
				       outline-hidden focus-visible:ring-2 focus-visible:ring-gray-900 dark:focus-visible:ring-accent group"
					style="animation-delay: {idx * 45}ms"
					on:click={() => onSelect({ type: 'prompt', data: prompt.content })}
				>
					<div class="flex flex-col text-left leading-snug">
						{#if prompt.title && prompt.title[0] !== ''}
							<div
								class="text-[13.5px] font-medium text-gray-800 group-hover:text-gray-950 dark:text-gray-100 dark:group-hover:text-white transition line-clamp-1"
							>
								{prompt.title[0]}
							</div>
							<div
								class="mt-0.5 text-xs text-gray-500 group-hover:text-gray-700 dark:text-gray-400 dark:group-hover:text-gray-300 font-normal line-clamp-1"
							>
								{prompt.title[1]}
							</div>
						{:else}
							<div
								class="text-[13.5px] font-medium text-gray-800 group-hover:text-gray-950 dark:text-gray-100 dark:group-hover:text-white transition line-clamp-1"
							>
								{prompt.content}
							</div>
							<div
								class="mt-0.5 text-xs text-gray-500 group-hover:text-gray-700 dark:text-gray-400 dark:group-hover:text-gray-300 font-normal line-clamp-1"
							>
								{$i18n.t('Prompt')}
							</div>
						{/if}
					</div>
				</button>
			{/each}
		</div>
	{/if}
</div>

<style>
	/* Waterfall animation for the suggestions */
	@keyframes fadeInUp {
		0% {
			opacity: 0;
			transform: translateY(6px);
		}
		100% {
			opacity: 1;
			transform: translateY(0);
		}
	}

	.waterfall {
		opacity: 0;
		animation-name: fadeInUp;
		animation-duration: 200ms;
		animation-fill-mode: forwards;
		animation-timing-function: ease;
	}
</style>
