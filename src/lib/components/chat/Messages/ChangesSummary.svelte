<script lang="ts">
	// Mologate: the files a finished response changed, with lines added and removed and each file's
	// diff (backend/open_webui/utils/change_sources.py). A file's row shows its diff; the folder
	// button opens it in the file browser.
	import { getContext } from 'svelte';
	import { showFileNavPath } from '$lib/stores';
	import { diffHunks, splitPath, type ChangeRecord } from '$lib/utils/changes';

	import ChevronDown from '$lib/components/icons/ChevronDown.svelte';
	import ChevronRight from '$lib/components/icons/ChevronRight.svelte';
	import DocumentDuplicate from '$lib/components/icons/DocumentDuplicate.svelte';
	import FolderOpen from '$lib/components/icons/FolderOpen.svelte';
	import FileTypeIcon from '$lib/components/chat/FileNav/FileTypeIcon.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import DiffBlock from './DiffBlock.svelte';

	const i18n: any = getContext('i18n');

	export let changes: ChangeRecord;

	let open = (changes?.total ?? 0) <= 4;
	let expanded: Record<string, boolean> = {};

	const openFile = (path: string) => showFileNavPath.set(`/home/user/${path}`);
</script>

{#if changes?.total}
	<div
		class="mt-3 w-full max-w-3xl overflow-hidden rounded-xl border border-gray-100 text-xs dark:border-gray-800"
		data-changes={changes.source ?? 'session'}
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
					{@const [dir, name] = splitPath(file.path)}
					{@const shown = !!expanded[file.path] && !!file.diff}
					<li>
						<div class="flex items-center hover:bg-gray-50 dark:hover:bg-gray-850">
							<button
								type="button"
								class="flex min-w-0 flex-1 items-center gap-2 py-1 pl-3 pr-1 text-left disabled:cursor-default"
								disabled={!file.diff && file.status === 'deleted'}
								title={file.path}
								aria-expanded={file.diff ? shown : undefined}
								on:click={() =>
									file.diff
										? (expanded = { ...expanded, [file.path]: !expanded[file.path] })
										: openFile(file.path)}
							>
								{#if file.diff}
									<ChevronRight
										className="size-3 shrink-0 text-gray-400 transition-transform {shown
											? 'rotate-90'
											: ''}"
										strokeWidth="2.5"
									/>
								{:else}
									<span class="size-3 shrink-0"></span>
								{/if}
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
							<Tooltip content={$i18n.t('Open in file browser')} placement="top" className="flex">
								<button
									type="button"
									class="mr-1.5 rounded-md p-1 text-gray-300 transition hover:bg-gray-100 hover:text-gray-700 disabled:invisible dark:text-gray-600 dark:hover:bg-gray-800 dark:hover:text-gray-200"
									aria-label={$i18n.t('Open in file browser')}
									disabled={file.status === 'deleted'}
									on:click={() => openFile(file.path)}
								>
									<FolderOpen className="size-3.5" strokeWidth="1.75" />
								</button>
							</Tooltip>
						</div>
						{#if shown}
							<div
								class="mx-3 mb-2 mt-0.5 max-h-96 overflow-auto rounded-lg border border-gray-100 text-[0.7rem] dark:border-gray-800"
							>
								<DiffBlock code={diffHunks(file.diff ?? '')} />
								{#if file.diff_truncated}
									<div
										class="border-t border-gray-100 px-3 py-1.5 text-gray-400 dark:border-gray-800 dark:text-gray-500"
									>
										{$i18n.t('The diff is shortened here. Open the file to see all of it.')}
									</div>
								{/if}
							</div>
						{/if}
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
