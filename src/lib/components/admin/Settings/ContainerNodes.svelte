<script lang="ts">
	// Mologate: the machines that run each chat's own container (docs/container-nodes.md).
	import { getContext, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';

	import { getContainerNodes, setContainerNodes, type ContainerNode } from '$lib/apis/containers';

	import Switch from '$lib/components/common/Switch.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import SensitiveInput from '$lib/components/common/SensitiveInput.svelte';
	import Plus from '$lib/components/icons/Plus.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';

	const i18n: any = getContext('i18n');

	let enable = true;
	let nodes: ContainerNode[] = [];
	let loaded = false;
	let adding = false;
	let draft = { id: '', name: '', url: '', key: '' };

	const load = async () => {
		try {
			const data = await getContainerNodes(localStorage.token);
			enable = data.enable;
			nodes = data.nodes;
		} catch (error) {
			toast.error(`${error}`);
		}
		loaded = true;
	};

	const save = async (next: ContainerNode[], nextEnable = enable) => {
		try {
			const data = await setContainerNodes(
				localStorage.token,
				nextEnable,
				next.map((node) => ({ ...node, key: node.key ?? '' }))
			);
			enable = data.enable;
			nodes = data.nodes;
			return true;
		} catch (error) {
			toast.error(`${error}`);
			await load();
			return false;
		}
	};

	const addNode = async () => {
		const id = (draft.id || draft.name || '')
			.trim()
			.toLowerCase()
			.replace(/[^a-z0-9._-]+/g, '-');
		if (!id || !draft.url.trim() || !draft.key.trim()) {
			toast.error($i18n.t('A node needs an ID, a URL and its key.'));
			return;
		}
		const ok = await save([
			...nodes,
			{
				id,
				name: draft.name.trim() || id,
				url: draft.url.trim(),
				key: draft.key.trim(),
				enabled: true
			}
		]);
		if (ok) {
			draft = { id: '', name: '', url: '', key: '' };
			adding = false;
		}
	};

	const gb = (bytes: number) => Math.round((bytes / 1024 ** 3) * 10) / 10;

	const meterColor = (ratio: number) =>
		ratio >= 0.9 ? 'bg-red-500' : ratio >= 0.7 ? 'bg-amber-400' : 'bg-gray-900 dark:bg-accent';

	const meters = (node: ContainerNode) => {
		const status = node.status;
		if (!status) return [];
		const memoryUsed = status.memory_total - status.memory_available;
		return [
			{
				label: $i18n.t('Containers'),
				ratio: status.active / Math.max(1, status.max_active),
				value: `${status.active} / ${status.max_active}`
			},
			{
				label: $i18n.t('Memory'),
				ratio: memoryUsed / Math.max(1, status.memory_total),
				value: `${gb(memoryUsed)} / ${gb(status.memory_total)} GB`
			},
			{
				label: $i18n.t('Load'),
				ratio: node.score ?? 0,
				value: `${Math.round((node.score ?? 0) * 100)}%`
			}
		];
	};

	const stateOf = (node: ContainerNode) =>
		!node.enabled
			? {
					label: $i18n.t('Off'),
					class: 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400'
				}
			: !node.status
				? {
						label: $i18n.t('Unreachable'),
						class: 'bg-red-50 text-red-600 dark:bg-red-500/10 dark:text-red-400'
					}
				: !node.status.accepting
					? {
							label: $i18n.t('Low on disk'),
							class: 'bg-amber-50 text-amber-700 dark:bg-amber-400/10 dark:text-amber-300'
						}
					: {
							label: $i18n.t('Online'),
							class: 'bg-green-50 text-green-700 dark:bg-green-500/10 dark:text-green-400'
						};

	onMount(load);
</script>

<div>
	<div class="mb-2 flex items-center justify-between">
		<div class="text-xs text-gray-600 dark:text-gray-400">{$i18n.t('Container nodes')}</div>

		<Tooltip content={enable ? $i18n.t('Enabled') : $i18n.t('Disabled')}>
			<Switch state={enable} on:change={(e) => save(nodes, e.detail)} />
		</Tooltip>
	</div>

	<div class="grid grid-cols-1 gap-2 lg:grid-cols-2">
		{#each nodes as node (node.id)}
			{@const status = node.status}
			{@const state = stateOf(node)}
			<div
				class="flex flex-col gap-2.5 rounded-2xl border border-gray-100 p-3 dark:border-gray-800 {node.enabled
					? ''
					: 'opacity-60'}"
			>
				<div class="flex items-start gap-2">
					<div class="min-w-0 flex-1">
						<div class="flex items-center gap-2">
							<span class="truncate text-sm font-medium text-gray-900 dark:text-gray-100"
								>{node.name}</span
							>
							<span class="shrink-0 rounded-full px-1.5 py-px text-[0.6875rem] {state.class}"
								>{state.label}</span
							>
						</div>
						<div class="truncate text-[0.6875rem] text-gray-400 dark:text-gray-500">
							{node.url}
						</div>
					</div>
					<Switch
						state={node.enabled}
						on:change={(e) =>
							save(nodes.map((n) => (n.id === node.id ? { ...n, enabled: e.detail } : n)))}
					/>
					<Tooltip content={$i18n.t('Remove')}>
						<button
							class="-mr-1 rounded-lg p-1 text-gray-400 hover:bg-black/5 hover:text-gray-900 dark:hover:bg-white/5 dark:hover:text-white"
							type="button"
							on:click={() => save(nodes.filter((n) => n.id !== node.id))}
						>
							<XMark className="size-3.5" />
						</button>
					</Tooltip>
				</div>

				{#if status}
					<div class="flex flex-col gap-1.5">
						{#each meters(node) as meter}
							<div class="grid grid-cols-[5rem_1fr_6.5rem] items-center gap-2 text-[0.6875rem]">
								<span class="text-gray-500 dark:text-gray-400">{meter.label}</span>
								<div class="h-1.5 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
									<div
										class="h-full rounded-full {meterColor(meter.ratio)}"
										style="width: {Math.min(100, Math.max(2, meter.ratio * 100))}%"
									></div>
								</div>
								<span class="text-right tabular-nums text-gray-600 dark:text-gray-300"
									>{meter.value}</span
								>
							</div>
						{/each}
					</div>

					<div class="flex flex-wrap gap-1 text-[0.6875rem]">
						<span
							class="rounded-md bg-gray-50 px-1.5 py-0.5 text-gray-600 dark:bg-gray-800/70 dark:text-gray-300"
							>{$i18n.t('{{count}} running', { count: status.containers.running })}</span
						>
						<span
							class="rounded-md bg-gray-50 px-1.5 py-0.5 text-gray-600 dark:bg-gray-800/70 dark:text-gray-300"
							>{$i18n.t('{{count}} paused', { count: status.containers.paused })}</span
						>
						<span
							class="rounded-md bg-gray-50 px-1.5 py-0.5 text-gray-600 dark:bg-gray-800/70 dark:text-gray-300"
							>{$i18n.t('{{count}} stopped', { count: status.containers.stopped })}</span
						>
						{#if status.features?.guards === false}
							<span
								class="rounded-md bg-amber-50 px-1.5 py-0.5 text-amber-700 dark:bg-amber-400/10 dark:text-amber-300"
								>{$i18n.t('No network guards')}</span
							>
						{/if}
						{#if status.features?.bandwidth === false}
							<span
								class="rounded-md bg-amber-50 px-1.5 py-0.5 text-amber-700 dark:bg-amber-400/10 dark:text-amber-300"
								>{$i18n.t('No bandwidth limit')}</span
							>
						{/if}
					</div>
				{/if}
			</div>
		{/each}

		{#if adding}
			<div class="flex flex-col gap-2 rounded-2xl border border-gray-200 p-3 dark:border-gray-700">
				<input
					class="w-full rounded-lg bg-transparent text-sm outline-hidden placeholder:text-gray-400"
					placeholder={$i18n.t('Name')}
					bind:value={draft.name}
				/>
				<input
					class="w-full rounded-lg bg-transparent text-xs outline-hidden placeholder:text-gray-400"
					placeholder="https://node-2.example.com"
					bind:value={draft.url}
				/>
				<SensitiveInput
					placeholder={$i18n.t('Node key (NODE_API_KEY)')}
					required={false}
					bind:value={draft.key}
				/>
				<div class="flex justify-end gap-1.5">
					<button
						class="rounded-full px-3 py-1 text-xs text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800"
						type="button"
						on:click={() => (adding = false)}
					>
						{$i18n.t('Cancel')}
					</button>
					<button
						class="rounded-full bg-black px-3 py-1 text-xs text-white hover:bg-gray-900 dark:bg-white dark:text-black dark:hover:bg-gray-100"
						type="button"
						on:click={addNode}
					>
						{$i18n.t('Add node')}
					</button>
				</div>
			</div>
		{:else}
			<button
				class="flex min-h-24 items-center justify-center gap-1.5 rounded-2xl border border-dashed border-gray-200 text-xs text-gray-500 transition hover:border-gray-300 hover:text-gray-800 dark:border-gray-800 dark:text-gray-500 dark:hover:border-gray-700 dark:hover:text-gray-200"
				type="button"
				on:click={() => (adding = true)}
			>
				<Plus className="size-3.5" />
				{loaded && nodes.length === 0
					? $i18n.t('Add the first container node')
					: $i18n.t('Add node')}
			</button>
		{/if}
	</div>
</div>
