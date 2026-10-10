<script lang="ts">
	import CodeExecutionModal from './CodeExecutionModal.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import Check from '$lib/components/icons/Check.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';
	import EllipsisHorizontal from '$lib/components/icons/EllipsisHorizontal.svelte';

	export let codeExecutions = [];

	let selectedCodeExecution = null;
	let showCodeExecutionModal = false;

	$: if (codeExecutions) {
		updateSelectedCodeExecution();
	}

	const updateSelectedCodeExecution = () => {
		if (selectedCodeExecution) {
			selectedCodeExecution = codeExecutions.find(
				(execution) => execution.id === selectedCodeExecution.id
			);
		}
	};
</script>

<CodeExecutionModal bind:show={showCodeExecutionModal} codeExecution={selectedCodeExecution} />

{#if codeExecutions.length > 0}
	<div class="mt-1 mb-2 w-full flex gap-1 items-center flex-wrap">
		{#each codeExecutions as execution (execution.id)}
			<div class="flex gap-1 text-xs font-normal">
				<button
					class="flex items-center text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-100 py-1 px-1.5 border border-gray-200 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-850 transition rounded-[7px] max-w-96"
					on:click={() => {
						selectedCodeExecution = execution;
						showCodeExecutionModal = true;
					}}
				>
					<div
						class="bg-gray-100 dark:bg-gray-800 rounded-full size-4 flex items-center justify-center"
					>
						{#if execution?.result}
							{#if execution.result?.error}
								<XMark />
							{:else if execution.result?.output}
								<Check strokeWidth="3" className="size-3" />
							{:else}
								<EllipsisHorizontal />
							{/if}
						{:else}
							<Spinner className="size-4" />
						{/if}
					</div>
					<div
						class="flex-1 mx-2 line-clamp-1 code-execution-name {execution?.result ? '' : 'pulse'}"
					>
						{execution.name}
					</div>
				</button>
			</div>
		{/each}
	</div>
{/if}

<style>
	@keyframes pulse {
		0%,
		100% {
			opacity: 1;
		}
		50% {
			opacity: 0.6;
		}
	}

	.pulse {
		opacity: 1;
		animation: pulse 1.5s ease;
	}
</style>
