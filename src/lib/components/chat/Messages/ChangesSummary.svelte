<script lang="ts">
	// Mologate: the files a finished response changed in the chat's container, with lines added
	// and removed (docs/container-nodes.md). Clicking a file opens it in the file browser.
	import { getContext } from 'svelte';
	import { showFileNavPath } from '$lib/stores';

	import ChevronDown from '$lib/components/icons/ChevronDown.svelte';
	import FileTypeIcon from '$lib/components/chat/FileNav/FileTypeIcon.svelte';
	import DocumentDuplicate from '$lib/components/icons/DocumentDuplicate.svelte';

	const i18n: any = getContext('i18n');

	export let changes: {
		files: { path: string; status: string; added: number | null; removed: number | null }[];
		total: number;
		added: number;
		removed: number;
		truncated?: boolean;
	};

	let open = (changes?.total ?? 0) <= 4;

	const openFile = (path: string) => showFileNavPath.set(`/home/user/${path}`);

	const split = (path: string) => {
		const index = path.lastIndexOf('/');
		return index < 0 ? ['', path] : [path.slice(0, index + 1), path.slice(index + 1)];
	};
</script>

{#if changes?.total}
	<div
		class="mt-3 w-full max-w-xl overflow-hidden rounded-xl border border-gray-100 text-xs dark:border-gray-800"
	>
		<button
			type="button"
			class="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-gray-50 dark:hover:bg-gray-850"
			aria-expanded={open}
			on:click={() => (open = !open)}
		>
			<DocumentDuplicate className="size-3.5 shrink-0 text-gray-500" strokeWidth="1.75" />
			<span class="text-gray-700 dark:text-gray-200">
				{$i18n.t('{{count}} files changed', { count: changes.total })}
			</span>
			<span class="font-mono tabular-nums text-green-600 dark:text-green-400">+{changes.added}</span
			>
			<span class="font-mono tabular-nums text-red-600 dark:text-red-400">−{changes.removed}</span>
			<ChevronDown
				className="ml-auto size-3.5 shrink-0 text-gray-400 transition-transform {open
					? 'rotate-180'
					: ''}"
				strokeWidth="2"
			/>
		</button>

		{#if open}
			<ul class="border-t border-gray-100 py-1 dark:border-gray-800">
				{#each changes.files as file (file.path)}
					{@const [dir, name] = split(file.path)}
					<li>
						<button
							type="button"
							class="flex w-full items-center gap-2 px-3 py-1 text-left hover:bg-gray-50 disabled:cursor-default disabled:hover:bg-transparent dark:hover:bg-gray-850"
							disabled={file.status === 'deleted'}
							title={file.path}
							on:click={() => openFile(file.path)}
						>
							<FileTypeIcon {name} size={14} />
							<span
								class="min-w-0 flex-1 truncate font-mono {file.status === 'deleted'
									? 'text-gray-400 line-through dark:text-gray-500'
									: 'text-gray-700 dark:text-gray-200'}"
								><span class="text-gray-400 dark:text-gray-500">{dir}</span>{name}</span
							>
							{#if file.added === null && file.removed === null}
								<span class="shrink-0 text-gray-400 dark:text-gray-500">{$i18n.t('binary')}</span>
							{:else}
								<span class="shrink-0 font-mono tabular-nums">
									{#if file.added}<span class="text-green-600 dark:text-green-400"
											>+{file.added}</span
										>{/if}
									{#if file.removed}<span class="ml-1 text-red-600 dark:text-red-400"
											>−{file.removed}</span
										>{/if}
								</span>
							{/if}
						</button>
					</li>
				{/each}
			</ul>
			{#if changes.truncated && changes.total > changes.files.length}
				<div class="px-3 pb-2 text-gray-400 dark:text-gray-500">
					{$i18n.t('and {{count}} more', { count: changes.total - changes.files.length })}
				</div>
			{/if}
		{/if}
	</div>
{/if}
