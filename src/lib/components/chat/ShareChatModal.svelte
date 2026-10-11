<script lang="ts">
	// Mologate: a chat is shared with the team in one of two ways, and both need a Mologate
	// account. "Open to the team" lets everyone signed in find the live chat under Team in the
	// sidebar and read it; a link shares a snapshot with whoever has the URL.
	import { getContext } from 'svelte';
	import { models, config, user } from '$lib/stores';

	import { toast } from 'svelte-sonner';
	import {
		deleteSharedChatById,
		getChatById,
		getChatTeamAccess,
		shareChatById,
		updateChatTeamAccess
	} from '$lib/apis/chats';
	import { copyToClipboard } from '$lib/utils';

	import Modal from '../common/Modal.svelte';
	import Switch from '../common/Switch.svelte';
	import Link from '../icons/Link.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';

	export let chatId;

	let chat: any = null;
	let teamOpen = false;
	let savingTeamAccess = false;
	const i18n: any = getContext('i18n');

	// Only the chat's creator shares it; a teammate reading it can still copy its address.
	$: canShare =
		chat?.user_id === $user?.id &&
		($user?.role === 'admin' || ($user?.permissions?.chat?.share ?? true));
	$: chatUrl = chatId ? `${window.location.origin}/c/${chatId}` : '';

	const shareLocalChat = async () => {
		const sharedChat = await shareChatById(localStorage.token, chatId);
		const shareUrl = `${window.location.origin}/s/${sharedChat.share_id}`;
		chat = await getChatById(localStorage.token, chatId);

		return shareUrl;
	};

	const shareChat = async () => {
		const _chat = chat.chat;
		console.log('share', _chat);

		// LICENSE covers this Open WebUI Community wordmark.
		// Do not alter, remove, obscure, or replace it except as LICENSE permits:
		// https://docs.openwebui.com/license.
		toast.success($i18n.t('Redirecting you to Open WebUI Community'));
		const url = 'https://openwebui.com';
		// const url = 'http://localhost:5173';

		const tab = await window.open(`${url}/chats/upload`, '_blank');
		window.addEventListener(
			'message',
			(event) => {
				if (event.origin !== url) return;
				if (event.data === 'loaded') {
					tab.postMessage(
						JSON.stringify({
							chat: _chat,
							models: $models.filter((m) => _chat.models.includes(m.id))
						}),
						'*'
					);
				}
			},
			false
		);
	};

	const setTeamOpen = async (open: boolean) => {
		savingTeamAccess = true;
		try {
			const res = await updateChatTeamAccess(localStorage.token, chatId, open);
			teamOpen = res?.open ?? open;
			toast.success(
				teamOpen ? $i18n.t('Chat opened to the team') : $i18n.t('Chat is private again')
			);
		} catch (e) {
			teamOpen = !open;
			toast.error(`${e}`);
		} finally {
			savingTeamAccess = false;
		}
	};

	const copyShareLink = async () => {
		const isSafari = /^((?!chrome|android).)*safari/i.test(navigator.userAgent);

		if (isSafari) {
			// Safari only allows clipboard writes started by the click itself.
			const getUrlPromise = async () => {
				const url = await shareLocalChat();
				return new Blob([url], { type: 'text/plain' });
			};

			navigator.clipboard
				.write([
					new ClipboardItem({
						'text/plain': getUrlPromise()
					})
				])
				.catch((error) => {
					console.error('Async: Could not copy text: ', error);
				});
		} else {
			copyToClipboard(await shareLocalChat());
		}

		toast.success($i18n.t('Copied shared chat URL to clipboard!'));
	};

	export let show = false;

	const isDifferentChat = (_chat: any) => {
		if (!chat) {
			return true;
		}
		if (!_chat) {
			return false;
		}
		return chat.id !== _chat.id || chat.share_id !== _chat.share_id;
	};

	$: if (show) {
		(async () => {
			if (chatId) {
				const _chat = await getChatById(localStorage.token, chatId);
				if (isDifferentChat(_chat)) {
					chat = _chat;
				}
				const access = await getChatTeamAccess(localStorage.token, chatId).catch(() => null);
				teamOpen = access?.open ?? false;
			} else {
				chat = null;
				teamOpen = false;
			}
		})();
	}
</script>

<Modal bind:show size="md">
	<div>
		<div class=" flex justify-between dark:text-gray-300 px-4 pt-3 pb-1">
			<div class=" text-sm font-medium self-center">{$i18n.t('Share Chat')}</div>
			<button
				class="self-center rounded-lg p-1 text-gray-500 transition hover:bg-gray-50 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-200"
				aria-label={$i18n.t('Close')}
				on:click={() => {
					show = false;
				}}
			>
				<XMark className={'size-4'} />
			</button>
		</div>

		{#if chat}
			<div class="px-5 pt-3 pb-5 w-full flex flex-col gap-4 text-sm dark:text-gray-300">
				<div class="flex flex-col gap-2">
					<div class="flex items-start justify-between gap-4">
						<div class="flex min-w-0 flex-col gap-0.5">
							<div id="share-chat-team-label" class="font-medium text-gray-900 dark:text-gray-100">
								{$i18n.t('Open to the team')}
							</div>
							<div class="text-xs text-gray-500 dark:text-gray-400">
								{$i18n.t(
									'Everyone signed in to Mologate can find this chat under Team in the sidebar and read it. Only you can send messages.'
								)}
							</div>
						</div>
						<div
							class="shrink-0 pt-0.5 {savingTeamAccess || !canShare
								? 'pointer-events-none opacity-50'
								: ''}"
						>
							<Switch
								ariaLabelledbyId="share-chat-team-label"
								bind:state={teamOpen}
								on:change={(e) => setTeamOpen(e.detail)}
							/>
						</div>
					</div>

					{#if teamOpen}
						<div class="flex items-center gap-2">
							<div
								class="min-w-0 flex-1 truncate rounded-lg bg-gray-50 px-2.5 py-1.5 font-mono text-xs text-gray-600 dark:bg-gray-850 dark:text-gray-400"
							>
								{chatUrl}
							</div>
							<button
								class="shrink-0 rounded-lg px-2.5 py-1.5 text-xs text-gray-700 transition hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800"
								type="button"
								on:click={() => {
									copyToClipboard(chatUrl);
									toast.success($i18n.t('Copied to clipboard'));
								}}
							>
								{$i18n.t('Copy')}
							</button>
						</div>
					{/if}
				</div>

				<div class="h-px bg-gray-100 dark:bg-gray-850"></div>

				<div class="flex flex-col gap-0.5">
					<div class="font-medium text-gray-900 dark:text-gray-100">{$i18n.t('Share link')}</div>
					<div class="text-xs text-gray-500 dark:text-gray-400">
						{$i18n.t(
							'Anyone signed in to Mologate who has the link can view the chat as it is now. Messages sent later show up once you update the link.'
						)}
					</div>
					{#if chat.share_id}
						<div class="mt-1 text-xs text-gray-500 dark:text-gray-400">
							<a class="underline" href="/s/{chat.share_id}" target="_blank"
								>{$i18n.t('You have shared this chat')}</a
							>.
							<button
								class="underline"
								on:click={async () => {
									const res = await deleteSharedChatById(localStorage.token, chatId);

									if (res) {
										chat = await getChatById(localStorage.token, chatId);
									}
								}}
								>{$i18n.t('Delete link')}
							</button>
						</div>
					{/if}
				</div>

				<div class="flex justify-end gap-1">
					{#if $config?.features.enable_community_sharing}
						<button
							class="flex items-center gap-1 px-3.5 py-2 text-sm font-normal bg-gray-100 hover:bg-gray-200 text-gray-800 dark:bg-gray-850 dark:text-white dark:hover:bg-gray-800 transition rounded-full"
							type="button"
							on:click={() => {
								shareChat();
							}}
						>
							<!-- LICENSE covers this Open WebUI Community wordmark.
							Do not alter, remove, obscure, or replace it except as LICENSE permits:
							https://docs.openwebui.com/license. -->
							{$i18n.t('Share to Open WebUI Community')}
						</button>
					{/if}

					<button
						class="flex items-center gap-1 px-3.5 py-2 text-sm font-normal bg-black hover:bg-gray-900 text-white dark:bg-white dark:text-black dark:hover:bg-gray-100 transition rounded-full disabled:opacity-50"
						type="button"
						id="copy-and-share-chat-button"
						disabled={!canShare}
						on:click={copyShareLink}
					>
						<Link />

						{#if chat.share_id}
							{$i18n.t('Update and Copy Link')}
						{:else}
							{$i18n.t('Copy Link')}
						{/if}
					</button>
				</div>
			</div>
		{/if}
	</div>
</Modal>
