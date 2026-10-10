<script lang="ts">
	// Mologate: other people's chats that are open to the team (see ShareChatModal). They open
	// read-only at their usual address; the creator's name sits on the right.
	import { getContext } from 'svelte';

	import { chatId, mobile, showSidebar } from '$lib/stores';
	import ChatIcon from './icons/Chat.svelte';

	const i18n: any = getContext('i18n');

	export let chats: {
		id: string;
		title: string;
		user_id: string;
		user_name?: string | null;
		updated_at: number;
	}[] = [];
	export let selectedChatId: any = null;
</script>

<div class="mt-0.5 pb-1 flex flex-col gap-px" id="team-chat-list">
	{#each chats as chat (chat.id)}
		<a
			class="flex h-7.5 items-center gap-2.5 rounded-[7px] pl-[0.6875rem] pr-1.5 text-[0.8125rem] leading-5 text-gray-700 transition hover:bg-gray-100 hover:text-gray-900 dark:text-gray-400 dark:hover:bg-gray-900 dark:hover:text-gray-100 {$chatId ===
			chat.id
				? 'bg-gray-100 text-gray-900 dark:bg-gray-900 dark:text-gray-100'
				: ''}"
			href={`/c/${chat.id}`}
			title={chat.user_name
				? $i18n.t('{{TITLE}} · by {{NAME}}', { TITLE: chat.title, NAME: chat.user_name })
				: chat.title}
			on:click={() => {
				selectedChatId = chat.id;
				if ($mobile) {
					showSidebar.set(false);
				}
			}}
			draggable="false"
		>
			<div class="self-center text-gray-500">
				<ChatIcon className="size-3.5" strokeWidth="1.5" />
			</div>
			<div class="min-w-0 flex-1 truncate">{chat.title}</div>
			{#if chat.user_name}
				<div class="max-w-[40%] shrink-0 truncate text-[0.6875rem] text-gray-500">
					{chat.user_name}
				</div>
			{/if}
		</a>
	{/each}
</div>
