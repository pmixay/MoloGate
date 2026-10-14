<script lang="ts">
	// Mologate: in a chat linked to a repository, what isn't committed yet, above the message box
	// (backend/open_webui/utils/change_sources.py). It shows a repository source's `workspace` diff
	// once one exists, else every response's changes added up. Chats without a repository show each
	// response's changes under it only (Messages/ChangesSummary.svelte).
	import { getContext } from 'svelte';
	import { showFileNavPath } from '$lib/stores';
	import { chatChanges, splitPath, type RepositoryLink } from '$lib/utils/changes';

	import ChevronDown from '$lib/components/icons/ChevronDown.svelte';
	import DocumentDuplicate from '$lib/components/icons/DocumentDuplicate.svelte';
	import FileTypeIcon from '$lib/components/chat/FileNav/FileTypeIcon.svelte';

	const i18n: any = getContext('i18n');

	export let history: any = null;
	export let repository: RepositoryLink | null = null;

	let open = false;

	$: totals = repository ? chatChanges(history) : null;
</script>

{#if repository && totals}
	<div
		class="mx-1 mb-1 overflow-hidden rounded-xl border border-gray-200 bg-white text-xs dark:border-gray-800 dark:bg-gray-850"
	>
		{#if open}
			<ul class="max-h-[30vh] overflow-y-auto border-b border-gray-100 py-1 dark:border-gray-800">
				{#each totals.files as file (file.path)}
					{@const [dir, name] = splitPath(file.path)}
					<li>
						<button
							type="button"
							class="flex w-full items-center gap-2 px-3 py-1 text-left hover:bg-gray-50 disabled:cursor-default disabled:hover:bg-transparent dark:hover:bg-gray-800/60"
							disabled={file.status === 'deleted'}
							title={file.path}
							on:click={() =>
								showFileNavPath.set(`${repository?.path ?? '/home/user'}/${file.path}`)}
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
		{/if}

		<button
			type="button"
			id="chat-changes-bar"
			class="flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-gray-50 dark:hover:bg-gray-800/60"
			aria-expanded={open}
			on:click={() => (open = !open)}
		>
			<DocumentDuplicate className="size-3.5 shrink-0 text-gray-500" strokeWidth="1.75" />
			<span class="text-gray-600 dark:text-gray-300">
				{$i18n.t('{{count}} files changed', { count: totals.total })}
			</span>
			<span class="font-mono tabular-nums text-green-600 dark:text-green-400">+{totals.added}</span>
			<span class="font-mono tabular-nums text-red-600 dark:text-red-400">−{totals.removed}</span>
			<span class="min-w-0 truncate font-mono text-gray-400 dark:text-gray-500">
				{repository.name}{repository.branch ? ` · ${repository.branch}` : ''}
			</span>
			<ChevronDown
				className="ml-auto size-3.5 shrink-0 text-gray-400 transition-transform {open
					? ''
					: 'rotate-180'}"
				strokeWidth="2"
			/>
		</button>
	</div>
{/if}
