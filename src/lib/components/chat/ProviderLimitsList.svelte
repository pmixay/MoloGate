<script lang="ts">
	// Mologate: the usage limits a model's provider account reported, as labelled bars. Shown in
	// the context indicator's panel and the limits ring's panel (utils/provider_limits.py).
	import { getContext } from 'svelte';

	import {
		amountText,
		formatPercent,
		limitLabel,
		limitLevel,
		resetText,
		type LimitWindow
	} from '$lib/utils/providerLimits';

	const i18n: any = getContext('i18n');

	export let windows: LimitWindow[] = [];

	$: t = (key: string, values?: Record<string, string | number>) => $i18n.t(key, values);
</script>

{#each windows as window (window.id)}
	{@const level = limitLevel(window.used_percent)}
	<div class="flex flex-col gap-1">
		<div class="flex items-baseline justify-between gap-3 text-[12px]">
			<span>{limitLabel(window, t)}</span>
			<span class="font-mono">
				{formatPercent(window.used_percent)}
				{#if level !== 'normal'}
					<span class="font-sans text-[11px] text-gray-500 dark:text-gray-400"
						>· {$i18n.t('running low')}</span
					>
				{/if}
			</span>
		</div>
		<div class="limits-track h-1.5 w-full overflow-hidden rounded-full">
			<div
				class="h-full rounded-full limits-{level}"
				style="width: {Math.max(window.used_percent, 2)}%"
			></div>
		</div>
		<div class="text-[11px] text-gray-500 dark:text-gray-400">
			{[amountText(window, t), resetText(window, t)].filter(Boolean).join(' · ')}
		</div>
	</div>
{/each}

<style>
	/* The context indicator's status colors: a window running low, or nearly full. */
	.limits-track {
		background: #e8e8ea;
	}
	.limits-normal {
		background: #3f3f46;
	}
	.limits-warning {
		background: #fab219;
	}
	.limits-critical {
		background: #d03b3b;
	}
	:global(.dark) .limits-track {
		background: #2a2b36;
	}
	:global(.dark) .limits-normal {
		background: #d4d4d8;
	}
</style>
