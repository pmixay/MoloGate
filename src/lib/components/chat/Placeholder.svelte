<script lang="ts">
	import { toast } from 'svelte-sonner';
	import { marked } from 'marked';
	import DOMPurify from 'dompurify';

	import { onMount, getContext, tick, createEventDispatcher } from 'svelte';
	import { blur, fade } from 'svelte/transition';

	const dispatch = createEventDispatcher();

	import { updateFolderById } from '$lib/apis/folders';

	import { user, models as _models, temporaryChatEnabled, selectedFolder } from '$lib/stores';
	import { refreshChatList, refreshFolderChatLists } from '$lib/stores/chatList';
	import { sanitizeResponseContent, extractCurlyBraceWords } from '$lib/utils';
	import {
		resolveLocalizedModelDescription,
		resolveLocalizedModelName
	} from '$lib/utils/localizedContent';
	import { WEBUI_API_BASE_URL, WEBUI_BASE_URL } from '$lib/constants';

	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import EyeSlash from '$lib/components/icons/EyeSlash.svelte';
	import MessageInput from './MessageInput.svelte';
	import FolderPlaceholder from './Placeholder/FolderPlaceholder.svelte';
	import FolderTitle from './Placeholder/FolderTitle.svelte';

	const i18n: any = getContext('i18n');

	export let createMessagePair: Function;
	export let stopResponse: Function;

	export let autoScroll = false;

	export let atSelectedModel: Model | undefined;
	export let selectedModels: [''];

	export let history;

	export let prompt = '';
	export let files = [];
	export let messageInput = null;

	export let selectedToolIds = [];
	export let selectedSkillIds = [];
	export let selectedFilterIds = [];
	export let pendingOAuthTools = [];
	export let params = {}; // Effort selector (agent C): passed through to the composer

	export let showCommands = false;

	export let alwaysAllowActions = false;
	export let onAlwaysAllowActionsChange: Function = () => {};
	export let oauthRedirectHandler: Function = () => {};

	export let onUpload: Function = (e) => {};
	export let onUpdate: (data?: { file?: any }) => void = () => {};
	export let onChange = (e) => {};
	export let messageQueue: { id: string; prompt: string; files: any[] }[] = [];
	export let onQueueSendNow: (id: string) => void = () => {};
	export let onQueueEdit: (id: string) => void = () => {};
	export let onQueueDelete: (id: string) => void = () => {};
	export let askUser = {
		show: false,
		questions: [],
		allowOther: true,
		timeoutMs: null,
		onConfirm: (_value: any) => {},
		onCancel: () => {}
	};

	export let dragged = false;

	let models = [];
	export let selectedModelIdx = 0;
	let selectedModel;
	let selectedModelName = '';
	let selectedModelDescription = '';

	$: models = selectedModels.map((id) => $_models.find((m) => m.id === id));
	$: selectedModel = atSelectedModel ?? models[selectedModelIdx];
	$: selectedModelName = resolveLocalizedModelName(selectedModel, $i18n.language);
	$: selectedModelDescription = resolveLocalizedModelDescription(selectedModel, $i18n.language);

	// True when viewing a shared folder the current user doesn't own AND lacks write access
	$: folderReadOnly =
		$selectedFolder != null &&
		$selectedFolder.user_id !== $user?.id &&
		!$selectedFolder.write_access;
</script>

<div class="m-auto w-full max-w-[58rem] px-1 @2xl:px-20 -translate-y-4 py-24 text-center">
	{#if $temporaryChatEnabled}
		<Tooltip
			content={$i18n.t("This chat won't appear in history and your messages will not be saved.")}
			className="w-full flex justify-center mb-0.5"
			placement="top"
		>
			<div
				class="flex items-center gap-1.5 rounded-md border border-dashed border-gray-300 px-2 py-0.5 font-mono text-[11px] text-gray-500 my-1 w-fit dark:border-gray-700"
			>
				<EyeSlash strokeWidth="2" className="size-3.5" />{$i18n.t('Temporary Chat')}
			</div>
		</Tooltip>
	{/if}

	<div class="w-full text-3xl text-gray-800 dark:text-gray-100 text-center flex items-center gap-4">
		<div class="w-full flex flex-col justify-center items-center">
			{#if $selectedFolder}
				<FolderTitle
					folder={$selectedFolder}
					readOnly={folderReadOnly}
					onUpdate={async () => {
						await Promise.all([refreshChatList(localStorage.token), refreshFolderChatLists(null)]);
					}}
					onDelete={async () => {
						await Promise.all([refreshChatList(localStorage.token), refreshFolderChatLists(null)]);

						selectedFolder.set(null);
					}}
				/>
			{:else}
				<div
					class="flex flex-row justify-center items-center gap-3 @sm:gap-3.5 w-fit px-5 max-w-xl"
				>
					<div class="flex shrink-0 justify-center">
						<div class="flex -space-x-4 mb-0.5" in:fade={{ duration: 100 }}>
							{#each models as model, modelIdx}
								<Tooltip
									content={(models[modelIdx]?.info?.meta?.tags ?? [])
										.map((tag) => tag.name.toUpperCase())
										.join(', ')}
									placement="top"
								>
									<button
										aria-hidden={models.length <= 1}
										aria-label={$i18n.t('Get information on {{name}} in the UI', {
											name: models[modelIdx]?.name
										})}
										on:click={() => {
											selectedModelIdx = modelIdx;
										}}
									>
										<img
											src={`${WEBUI_API_BASE_URL}/models/model/profile/image?id=${model?.id}&lang=${$i18n.language}`}
											class=" size-9 @sm:size-10 rounded-[10px] ring-1 ring-gray-200 dark:ring-gray-800"
											aria-hidden="true"
											draggable="false"
											on:error={(e) => {
												// LICENSE covers this Open WebUI fallback logo.
												// Do not alter, remove, obscure, or replace it except as LICENSE permits:
												// https://docs.openwebui.com/license.
												e.currentTarget.src = '/favicon.png';
											}}
										/>
									</button>
								</Tooltip>
							{/each}
						</div>
					</div>

					<div
						class=" text-2xl @sm:text-[1.625rem] tracking-tight line-clamp-1 flex items-center text-gray-900 dark:text-gray-100"
						in:fade={{ duration: 100 }}
					>
						{#if selectedModelName}
							<Tooltip content={selectedModelName} placement="top" className=" flex items-center ">
								<span class="line-clamp-1 font-mono font-medium tracking-[-0.03em]">
									{selectedModelName}
								</span>
							</Tooltip>
						{:else}
							{$i18n.t('Hello, {{name}}', { name: $user?.name })}
						{/if}
					</div>
				</div>

				<div class="flex mt-1.5 mb-2">
					<div in:fade={{ duration: 100, delay: 50 }}>
						{#if selectedModelDescription}
							<Tooltip
								className=" w-fit"
								content={DOMPurify.sanitize(
									marked.parse(
										sanitizeResponseContent(selectedModelDescription).replaceAll('\n', '<br>')
									)
								)}
								placement="top"
							>
								<div
									class="mt-0.5 px-2 text-[13.5px] font-normal text-gray-500 dark:text-gray-400 line-clamp-2 max-w-xl markdown"
								>
									{@html DOMPurify.sanitize(
										marked.parse(
											sanitizeResponseContent(selectedModelDescription).replaceAll('\n', '<br>')
										)
									)}
								</div>
							</Tooltip>

							{#if models[selectedModelIdx]?.info?.meta?.user}
								<div class="mt-0.5 text-[12.5px] font-normal text-gray-500">
									{$i18n.t('By')}
									{#if models[selectedModelIdx]?.info?.meta?.user.community}
										<a
											href="https://openwebui.com/m/{models[selectedModelIdx]?.info?.meta?.user
												.username}"
											>{models[selectedModelIdx]?.info?.meta?.user.name
												? models[selectedModelIdx]?.info?.meta?.user.name
												: `@${models[selectedModelIdx]?.info?.meta?.user.username}`}</a
										>
									{:else}
										{models[selectedModelIdx]?.info?.meta?.user.name}
									{/if}
								</div>
							{/if}
						{/if}
					</div>
				</div>
			{/if}

			<div
				class="text-base font-normal @md:max-w-3xl w-full pt-4 pb-3 {atSelectedModel ? 'mt-2' : ''}"
			>
				{#if !($selectedFolder && folderReadOnly)}
					<MessageInput
						bind:this={messageInput}
						{history}
						bind:selectedModels
						bind:params
						bind:files
						bind:prompt
						bind:autoScroll
						bind:selectedToolIds
						bind:selectedSkillIds
						bind:selectedFilterIds
						bind:atSelectedModel
						bind:showCommands
						bind:dragged
						{pendingOAuthTools}
						{oauthRedirectHandler}
						{alwaysAllowActions}
						{onAlwaysAllowActionsChange}
						{stopResponse}
						{createMessagePair}
						placeholder={$i18n.t('How can I help you today?')}
						{onChange}
						{onUpload}
						{onUpdate}
						{messageQueue}
						{onQueueSendNow}
						{onQueueEdit}
						{onQueueDelete}
						{askUser}
						on:chatVariables
						on:submit={(e) => {
							dispatch('submit', e.detail);
						}}
					/>
				{/if}
			</div>
		</div>
	</div>

	{#if $selectedFolder}
		<div class="mx-auto px-4 md:max-w-3xl md:px-6 min-h-62" in:fade={{ duration: 200, delay: 200 }}>
			<FolderPlaceholder folder={$selectedFolder} />
		</div>
	{/if}
</div>
