<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { secondsToMs, toolElapsedMs, type Seconds } from './toolElapsed';

	export let startedAt: Seconds = undefined;
	export let endedAt: Seconds = undefined;
	export let duration: Seconds = undefined;
	export let done: boolean | string = false;

	let mounted = false;
	let now = Date.now();
	let timer: ReturnType<typeof setInterval> | undefined;
	let frozenNow: number | undefined;
	$: complete = done === true || done === 'true';
	$: active = !complete && secondsToMs(startedAt) !== null && secondsToMs(endedAt) === null;
	$: if (mounted && active && !timer) {
		now = Date.now();
		frozenNow = undefined;
		timer = setInterval(() => (now = Date.now()), 1000);
	} else if (!active && timer) {
		clearInterval(timer);
		timer = undefined;
		frozenNow = Date.now();
	}
	$: elapsed = toolElapsedMs(startedAt, endedAt, duration, complete, frozenNow ?? now);
	$: label = elapsed === null ? '' : `${Math.floor(elapsed / 1000)}s`;
	onMount(() => {
		mounted = true;
	});
	onDestroy(() => {
		if (timer) clearInterval(timer);
	});
</script>

{#if label}
	<span
		class="text-xs text-gray-500 dark:text-gray-400 tabular-nums whitespace-nowrap"
		aria-label="Elapsed time">{label}</span
	>
{/if}
