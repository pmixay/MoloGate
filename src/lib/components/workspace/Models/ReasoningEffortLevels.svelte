<script lang="ts">
	// Admin override for the effort levels a model offers in the chat effort selector.
	// Stored as `meta.reasoning_effort_levels`; empty means "use what the provider reports".
	import { getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';

	import { models } from '$lib/stores';
	import Checkbox from '$lib/components/common/Checkbox.svelte';

	const i18n: Writable<i18nType> = getContext('i18n');

	const STANDARD_LEVELS = ['minimal', 'low', 'medium', 'high', 'xhigh'];

	export let levels: string[] = [];
	export let modelId = '';
	export let baseModelId: string | null = null;

	// Keep levels saved outside the standard set (e.g. through the API) visible and editable.
	$: choices = [...STANDARD_LEVELS, ...levels.filter((level) => !STANDARD_LEVELS.includes(level))];

	// What the selector would offer without an override: the provider's levels, or the base model's.
	$: detected = (() => {
		const own = $models.find((m: any) => m.id === modelId)?.reasoning_effort_options;
		if (own && !baseModelId) {
			return own.source === 'admin'
				? (own.detected ?? [])
				: (own.levels ?? []).map((l: any) => l.value);
		}
		const base = $models.find((m: any) => m.id === baseModelId)?.reasoning_effort_options;
		return (base?.levels ?? []).map((l: any) => l.value);
	})();

	const setLevel = (level: string, checked: boolean) => {
		const next = new Set(levels.filter((l) => l !== level));
		if (checked) next.add(level);
		levels = choices.filter((l) => next.has(l));
	};
</script>

<div>
	<div class="mb-1.5 text-xs text-gray-400 dark:text-gray-600">
		{$i18n.t('Reasoning effort levels')}
	</div>
	<div class="grid grid-cols-2 gap-x-5 gap-y-1 sm:grid-cols-3 lg:grid-cols-5">
		{#each choices as level (level)}
			<div class="flex min-h-6 items-center gap-2.5">
				<Checkbox
					ariaLabel={level}
					state={levels.includes(level) ? 'checked' : 'unchecked'}
					on:change={(e) => setLevel(level, e.detail === 'checked')}
				/>
				<button
					type="button"
					class="min-w-0 cursor-pointer truncate text-left font-mono text-xs text-gray-600 dark:text-gray-400"
					on:click={() => setLevel(level, !levels.includes(level))}
				>
					{level}
				</button>
			</div>
		{/each}
	</div>
	<div class="mt-1.5 text-[0.6875rem] leading-4 text-gray-500">
		{#if levels.length > 0}
			{$i18n.t('The effort selector offers only the levels checked here.')}
		{:else}
			{$i18n.t('None checked: levels come from the provider automatically.')}
		{/if}
		{#if detected.length > 0}
			{$i18n.t('Provider reports: {{levels}}.', { levels: detected.join(', ') })}
		{:else}
			{$i18n.t('The provider reports none, so the selector is hidden unless levels are checked.')}
		{/if}
	</div>
</div>
