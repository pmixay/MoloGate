<script lang="ts">
	import type { TaskType } from '$lib/apis/chatTasks';

	import ClockRotateRight from '$lib/components/icons/ClockRotateRight.svelte';
	import Eye from '$lib/components/icons/Eye.svelte';
	import Sparkles from '$lib/components/icons/Sparkles.svelte';

	export let type: TaskType;
	export let className = 'size-4';
</script>

{#if type === 'monitor'}
	<Eye {className} strokeWidth="1.5" />
{:else if type === 'scheduled'}
	<ClockRotateRight {className} strokeWidth="1.5" />
{:else}
	<Sparkles {className} strokeWidth="1.5" />
{/if}
