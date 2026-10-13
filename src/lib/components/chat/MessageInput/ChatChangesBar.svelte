<script lang="ts">
	// Mologate: every file the agent changed in this conversation, with lines added and removed,
	// above the message box. Each finished response records its own changes
	// (Messages/ChangesSummary.svelte); this adds them up along the current branch.
	import { getContext } from 'svelte';
	import { showFileNavPath } from '$lib/stores';

	import ChevronDown from '$lib/components/icons/ChevronDown.svelte';
	import DocumentDuplicate from '$lib/components/icons/DocumentDuplicate.svelte';
	import FileTypeIcon from '$lib/components/chat/FileNav/FileTypeIcon.svelte';

	const i18n: any = getContext('i18n');

	export let history: any = null;

	type FileChange = { path: string; status: string; added: number | null; removed: number | null };

	let open = false;

	const collect = (history: any) => {
		const branch: any[] = [];
		let id = history?.currentId;
		for (let steps = 0; id && steps < 10000; steps += 1) {
			const message = history.messages?.[id];
			if (!message) break;
			if (message.changes?.total) branch.push(message.changes);
			id = message.parentId;
		}
		const files = new Map<string, FileChange>();
		let added = 0;
		let removed = 0;
		for (const changes of branch.reverse()) {
			added += changes.added ?? 0;
			removed += changes.removed ?? 0;
			for (const file of changes.files ?? []) {
				const seen = files.get(file.path);
				files.set(file.path, {
					path: file.path,
					// A file the chat created stays "added" however often it is edited afterwards.
					status: seen?.status === 'added' && file.status !== 'deleted' ? 'added' : file.status,
					added:
						seen?.added === null || file.added === null
							? null
							: (seen?.added ?? 0) + (file.added ?? 0),
					removed:
						seen?.removed === null || file.removed === null
							? null
							: (seen?.removed ?? 0) + (file.removed ?? 0)
				});
			}
		}
		return files.size
			? {
					files: [...files.values()].sort((a, b) => a.path.localeCompare(b.path)),
					added,
					removed
				}
			: null;
	};

	$: totals = collect(history);

	const split = (path: string) => {
		const index = path.lastIndexOf('/');
		return index < 0 ? ['', path] : [path.slice(0, index + 1), path.slice(index + 1)];
	};
</script>

{#if totals}
	<div
		class="mx-1 mb-1 overflow-hidden rounded-xl border border-gray-200 bg-white text-xs dark:border-gray-800 dark:bg-gray-850"
	>
		{#if open}
			<ul class="max-h-[30vh] overflow-y-auto border-b border-gray-100 py-1 dark:border-gray-800">
				{#each totals.files as file (file.path)}
					{@const [dir, name] = split(file.path)}
					<li>
						<button
							type="button"
							class="flex w-full items-center gap-2 px-3 py-1 text-left hover:bg-gray-50 disabled:cursor-default disabled:hover:bg-transparent dark:hover:bg-gray-800/60"
							disabled={file.status === 'deleted'}
							title={file.path}
							on:click={() => showFileNavPath.set(`/home/user/${file.path}`)}
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
				{$i18n.t('{{count}} files changed in this chat', { count: totals.files.length })}
			</span>
			<span class="font-mono tabular-nums text-green-600 dark:text-green-400">+{totals.added}</span>
			<span class="font-mono tabular-nums text-red-600 dark:text-red-400">−{totals.removed}</span>
			<ChevronDown
				className="ml-auto size-3.5 shrink-0 text-gray-400 transition-transform {open
					? ''
					: 'rotate-180'}"
				strokeWidth="2"
			/>
		</button>
	</div>
{/if}
