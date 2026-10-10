<script>
	import Sortable from 'sortablejs';

	import { onMount, getContext, tick } from 'svelte';

	import { chatId, mobile, pinnedNotes, settings, showSidebar } from '$lib/stores';
	import { updateUserSettings } from '$lib/apis/users';
	import { getPinnedNoteList, toggleNotePinnedStatusById } from '$lib/apis/notes';
	import NotesIcon from './icons/Notes.svelte';
	import XMarkIcon from './icons/XMark.svelte';

	const i18n = getContext('i18n');

	export let selectedChatId = null;

	$: sortedPinnedNotes = (() => {
		const order = $settings?.pinnedNotesOrder;
		if (!order || order.length === 0) return $pinnedNotes;
		const orderMap = new Map(order.map((id, idx) => [id, idx]));
		return [...$pinnedNotes].sort((a, b) => {
			const aIdx = orderMap.has(a.id) ? orderMap.get(a.id) : Infinity;
			const bIdx = orderMap.has(b.id) ? orderMap.get(b.id) : Infinity;
			return aIdx - bIdx;
		});
	})();

	const initPinnedNotesSortable = () => {
		const pinnedNotesList = document.getElementById('pinned-notes-list');
		if (pinnedNotesList && !$mobile) {
			new Sortable(pinnedNotesList, {
				animation: 150,
				setData: function (dataTransfer, dragEl) {
					dataTransfer.setData(
						'text/plain',
						JSON.stringify({
							type: 'note',
							id: dragEl.dataset.id
						})
					);
					dataTransfer.setData('application/x-open-webui-drag', '');
				},
				onUpdate: async (event) => {
					const noteId = event.item.dataset.id;
					const newIndex = event.newIndex;
					const current = sortedPinnedNotes.map((n) => n.id);
					const oldIndex = current.indexOf(noteId);
					current.splice(oldIndex, 1);
					current.splice(newIndex, 0, noteId);
					settings.set({ ...$settings, pinnedNotesOrder: current });
					await updateUserSettings(localStorage.token, { ui: { pinnedNotesOrder: current } });
				}
			});
		}
	};

	onMount(async () => {
		await tick();
		initPinnedNotesSortable();
	});
</script>

<div class="mt-0.5 pb-1 flex flex-col gap-px" id="pinned-notes-list">
	{#each sortedPinnedNotes as note (note.id)}
		<!-- svelte-ignore a11y-no-static-element-interactions -->
		<div
			class="flex h-7.5 items-center text-gray-700 dark:text-gray-400 cursor-grab relative group rounded-[7px] pl-[0.6875rem] pr-1.5 hover:bg-gray-100 hover:text-gray-900 dark:hover:bg-gray-900 dark:hover:text-gray-100 transition"
			data-id={note.id}
		>
			<a
				class="grow flex min-w-0 items-center gap-2.5 rounded-[5px] text-[0.8125rem] leading-5 outline-none focus-visible:ring-2 focus-visible:ring-accent"
				href={`/notes/${note.id}`}
				on:click={() => {
					selectedChatId = null;
					chatId.set('');
					if ($mobile) {
						showSidebar.set(false);
					}
				}}
				draggable="false"
			>
				<div class="self-center text-gray-500">
					<NotesIcon className="size-3.5" strokeWidth="1.5" />
				</div>
				<div class="flex-1 text-ellipsis line-clamp-1">
					{note.title}
				</div>
			</a>
			<button
				class="hover-reveal self-center p-0.5 text-gray-500 hover:bg-gray-200 hover:text-gray-900 dark:hover:bg-gray-800 dark:hover:text-gray-100 rounded-[5px] transition outline-none focus-visible:ring-2 focus-visible:ring-accent"
				on:click|preventDefault|stopPropagation={async () => {
					await toggleNotePinnedStatusById(localStorage.token, note.id);
					const _pinnedNotes = await getPinnedNoteList(localStorage.token).catch(() => []);
					pinnedNotes.set(_pinnedNotes);
				}}
				aria-label={$i18n.t('Unpin')}
			>
				<XMarkIcon className="size-3.5" strokeWidth="1.5" />
			</button>
		</div>
	{/each}
</div>
