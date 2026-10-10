<script lang="ts">
	import { createEventDispatcher, onDestroy, onMount } from 'svelte';

	import Collapsible from '$lib/components/common/Collapsible.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import ChevronRight from './icons/ChevronRight.svelte';
	import Plus from './icons/Plus.svelte';

	const dispatch = createEventDispatcher();
	type AddHandler = () => void | Promise<void>;

	export let open = true;
	export let id = '';
	export let name = '';
	export let collapsible = true;
	export let className = '';
	export let buttonClassName = '';
	export let contentClassName = 'px-2';
	export let onAddLabel = '';
	export let onAdd: null | AddHandler = null;
	export let dragAndDrop = true;

	let sectionElement: HTMLDivElement;
	let loaded = false;
	let draggedOver = false;

	const setOpen = (state: boolean) => {
		open = state;
		dispatch('change', state);
		localStorage.setItem(`${id}-folder-state`, `${state}`);
	};

	const onDragOver = (e: DragEvent) => {
		e.preventDefault();
		e.stopPropagation();
		draggedOver = true;
	};

	const onDrop = (e: DragEvent) => {
		e.preventDefault();
		e.stopPropagation();

		if (!sectionElement.contains(e.target as Node)) {
			return;
		}

		if (e.dataTransfer?.items && e.dataTransfer.items.length > 0) {
			for (const item of Array.from(e.dataTransfer.items)) {
				if (item.kind === 'file') {
					const file = item.getAsFile();
					if (file && file.type === 'application/json') {
						const reader = new FileReader();
						reader.onload = async function (event) {
							try {
								const fileContent = JSON.parse(event.target?.result as string);
								open = true;
								dispatch('import', fileContent);
							} catch (error) {
								console.error('Error parsing JSON file:', error);
							}
						};
						reader.readAsText(file);
					}
				} else {
					open = true;
					try {
						const dataTransfer = e.dataTransfer.getData('text/plain');
						if (dataTransfer) {
							dispatch('drop', JSON.parse(dataTransfer));
						}
					} catch {
						// Ignore drops that are not sidebar JSON payloads.
					} finally {
						draggedOver = false;
					}
					break;
				}
			}
		}

		draggedOver = false;
	};

	const onDragLeave = (e: DragEvent) => {
		e.preventDefault();
		e.stopPropagation();
		draggedOver = false;
	};

	onMount(() => {
		const state = localStorage.getItem(`${id}-folder-state`);
		if (state !== null) {
			open = state === 'true';
		}

		loaded = true;

		if (!dragAndDrop) {
			return;
		}

		sectionElement.addEventListener('dragover', onDragOver);
		sectionElement.addEventListener('drop', onDrop);
		sectionElement.addEventListener('dragleave', onDragLeave);
	});

	onDestroy(() => {
		if (!dragAndDrop) {
			return;
		}

		sectionElement.removeEventListener('dragover', onDragOver);
		sectionElement.removeEventListener('drop', onDrop);
		sectionElement.removeEventListener('dragleave', onDragLeave);
	});
</script>

<div bind:this={sectionElement} class="relative {className}">
	{#if loaded}
		{#if draggedOver}
			<div
				class="absolute inset-0 z-50 rounded-lg bg-gray-100/50 ring-1 ring-inset ring-gray-300 pointer-events-none touch-none dark:bg-gray-900/40 dark:ring-gray-700/70"
			></div>
		{/if}

		{#if collapsible}
			<Collapsible bind:open className="w-full" buttonClassName="w-full" onChange={setOpen}>
				<div class="flex items-center justify-between h-[1.375rem] w-full pl-[1.1875rem] pr-2.5 shrink-0">
					<button
						type="button"
						class="group flex flex-1 h-full min-w-0 items-center gap-1 text-left font-mono text-[0.65625rem] font-medium uppercase tracking-[0.08em] text-gray-600 hover:text-gray-900 dark:text-gray-500 dark:hover:text-gray-300 transition-colors duration-100 rounded-[5px] outline-none focus-visible:ring-2 focus-visible:ring-accent {buttonClassName}"
						aria-expanded={open}
						aria-controls="{id}-content"
					>
						<span class="truncate">{name}</span>
						<span
							class="flex opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 transition-all duration-100"
							style="transform: rotate({open ? '90deg' : '0deg'})"
						>
							<ChevronRight className="size-[0.6875rem]" />
						</span>
					</button>

					<!-- svelte-ignore a11y-click-events-have-key-events a11y-no-static-element-interactions -->
					<div class="contents" on:click={(e) => e.stopPropagation()}>
						<slot name="action" />
					</div>

					{#if onAdd}
						<button
							type="button"
							class="flex items-center justify-center size-[1.375rem] rounded-[5px] text-gray-500 hover:bg-gray-100 hover:text-gray-900 dark:hover:bg-gray-900 dark:hover:text-gray-100 transition-colors duration-100 outline-none focus-visible:ring-2 focus-visible:ring-accent"
							aria-label={onAddLabel}
							on:click={(e) => {
								e.stopPropagation();
								onAdd();
							}}
						>
							<Tooltip content={onAddLabel}>
								<Plus className="size-3.5" />
							</Tooltip>
						</button>
					{/if}
				</div>

				<div id="{id}-content" slot="content" class="w-full {contentClassName}">
					<slot></slot>
				</div>
			</Collapsible>
		{:else}
			<slot></slot>
		{/if}
	{/if}
</div>
