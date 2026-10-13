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

	const formatBytes = (bytes: number) => `${Math.round((bytes / 1024 ** 3) * 10) / 10} GB`;

	const describe = (node: ContainerNode) => {
		const status = node.status;
		if (!status) return '';
		return [
			$i18n.t('{{active}} of {{max}} containers active', {
				active: status.active,
				max: status.max_active
			}),
			$i18n.t('{{count}} stopped', { count: status.containers.stopped }),
			$i18n.t('{{size}} RAM free', { size: formatBytes(status.memory_available) }),
			$i18n.t('load {{percent}}%', { percent: Math.round((node.score ?? 0) * 100) }),
			...(status.features?.bandwidth === false ? [$i18n.t('no bandwidth limit')] : []),
			...(status.features?.guards === false ? [$i18n.t('no network guards')] : [])
		].join(' · ');
	};

	onMount(load);
</script>

<div>
	<div class="mb-2 flex items-center justify-between">
		<div class="text-xs text-gray-600 dark:text-gray-400">{$i18n.t('Container nodes')}</div>

		<div class="flex items-center gap-1">
			<Tooltip content={enable ? $i18n.t('Enabled') : $i18n.t('Disabled')}>
				<Switch state={enable} on:change={(e) => save(nodes, e.detail)} />
			</Tooltip>
			<Tooltip content={$i18n.t('Add node')}>
				<button
					class="flex size-6 items-center justify-center rounded-lg text-gray-400 transition-colors hover:bg-black/5 hover:text-gray-900 dark:text-gray-600 dark:hover:bg-white/5 dark:hover:text-white"
					on:click={() => (adding = !adding)}
					type="button"
				>
					<Plus />
				</button>
			</Tooltip>
		</div>
	</div>

	<div class="flex flex-col gap-1.5">
		{#each nodes as node (node.id)}
			{@const status = node.status}
			<div class="flex w-full items-center gap-2 {node.enabled ? '' : 'opacity-50'}">
				<Tooltip
					content={!node.enabled
						? $i18n.t('Disabled')
						: status
							? status.accepting
								? $i18n.t('Online')
								: $i18n.t('Online, low on disk: takes no new containers')
							: $i18n.t('Unreachable')}
				>
					<span
						class="block size-2 shrink-0 rounded-full {!node.enabled
							? 'bg-gray-400'
							: status
								? status.accepting
									? 'bg-green-500'
									: 'bg-amber-400'
								: 'bg-red-500'}"
					></span>
				</Tooltip>
				<div class="min-w-0 flex-1">
					<div class="truncate text-xs text-gray-700 dark:text-gray-300">
						{node.name} <span class="text-gray-400 dark:text-gray-600">{node.url}</span>
					</div>
					{#if status}
						<div class="truncate text-[0.6875rem] text-gray-400 dark:text-gray-500">
							{describe(node)}
						</div>
					{/if}
				</div>
				<Switch
					state={node.enabled}
					on:change={(e) =>
						save(nodes.map((n) => (n.id === node.id ? { ...n, enabled: e.detail } : n)))}
				/>
				<Tooltip content={$i18n.t('Remove')}>
					<button
						class="p-1 rounded-lg text-gray-400 hover:bg-black/5 hover:text-gray-900 dark:hover:bg-white/5 dark:hover:text-white"
						type="button"
						on:click={() => save(nodes.filter((n) => n.id !== node.id))}
					>
						<XMark className="size-3.5" />
					</button>
				</Tooltip>
			</div>
		{/each}
	</div>

	{#if loaded && nodes.length === 0 && !adding}
		<div class="text-[0.6875rem] text-gray-400 dark:text-gray-600">
			{$i18n.t('No container nodes yet, so chats have no container.')}
		</div>
	{/if}

	{#if adding}
		<div
			class="mt-2 flex flex-col gap-1.5 rounded-xl border border-gray-100 p-2 dark:border-gray-800"
		>
			<div class="flex gap-1.5">
				<input
					class="w-1/3 rounded-lg bg-transparent text-xs outline-hidden placeholder:text-gray-400"
					placeholder={$i18n.t('Name')}
					bind:value={draft.name}
				/>
				<input
					class="flex-1 rounded-lg bg-transparent text-xs outline-hidden placeholder:text-gray-400"
					placeholder="https://node-2.example.com"
					bind:value={draft.url}
				/>
			</div>
			<SensitiveInput
				placeholder={$i18n.t('Node key (NODE_API_KEY)')}
				required={false}
				bind:value={draft.key}
			/>
			<div class="flex justify-end">
				<button
					class="rounded-full bg-black px-3 py-1 text-xs text-white hover:bg-gray-900 dark:bg-white dark:text-black dark:hover:bg-gray-100"
					type="button"
					on:click={addNode}
				>
					{$i18n.t('Add node')}
				</button>
			</div>
		</div>
	{/if}

	<div class="mt-1 text-[0.6875rem] text-gray-400 dark:text-gray-600">
		{$i18n.t(
			"Each chat gets its own container on one of these machines. New chats go to the least busy node, and a stopped container moves to a quieter one when its node is busy. A node can run on the site's machine or any other."
		)}
	</div>
</div>
