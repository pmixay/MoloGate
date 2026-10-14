<script lang="ts">
	import { onMount, tick, getContext } from 'svelte';
	import {
		config,
		terminalServers,
		showControls,
		showArtifacts,
		showEmbeds,
		settings,
		showFileNavPath,
		selectedTerminalId,
		user,
		chatControlsTab,
		showTasks
	} from '$lib/stores';

	import Drawer from '../common/Drawer.svelte';
	import ResizableSidePanel from '../common/ResizableSidePanel.svelte';
	import Artifacts from './Artifacts.svelte';
	import Embeds from './ChatControls/Embeds.svelte';
	import FileNav from './FileNav.svelte';
	import PyodideFileNav from './PyodideFileNav.svelte';
	import Overview from './Overview.svelte';
	import TasksPanel from './Tasks/TasksPanel.svelte';
	import { chatTasks } from './Tasks/tasksStore';
	import { isSavedChatId } from '$lib/utils/chatId';

	const i18n: any = getContext('i18n');

	// Mologate: this side panel no longer has a Controls tab (chat parameters) and no voice-call
	// overlay. It only opens for artifacts, embeds, terminal/code-interpreter files, the chat's
	// tasks and the chat overview, each from its own entry point.
	export let history;

	export let chatId = null;
	export let chatUser = null;

	export let showMessage: Function;

	export let codeInterpreterEnabled = false;

	let largeScreen = false;
	let dragged = false;
	let mounted = false;
	let controlsWidth = 350;

	// Mologate: the Files / Tasks / Overview tab is $chatControlsTab, which the header buttons
	// also set; it stays as it was when another chat opens.

	$: hasMessages = history?.messages && Object.keys(history.messages).length > 0;

	const chatContext = (terminal: any) => terminal?.contexts?.chat ?? {};
	const chatContextAvailable = (terminal: any) => chatContext(terminal) !== false;
	const chatContextNeedsSavedChat = (terminal: any) =>
		chatContext(terminal)?.context_id === 'chat_id';
	$: selectedSystemTerminal = ($terminalServers ?? []).find(
		(t) => t.id && t.id === $selectedTerminalId
	);
	$: selectedSystemTerminalAvailable =
		selectedSystemTerminal &&
		chatContextAvailable(selectedSystemTerminal) &&
		!(chatContextNeedsSavedChat(selectedSystemTerminal) && !isSavedChatId(chatId));
	$: terminalFilesAvailable = !!(
		$selectedTerminalId &&
		(selectedSystemTerminalAvailable ||
			(!selectedSystemTerminal &&
				($user?.role === 'admin' || ($user?.permissions?.features?.direct_tool_servers ?? true))))
	);
	$: showFilesTab =
		terminalFilesAvailable ||
		(codeInterpreterEnabled && $config?.code?.interpreter_engine !== 'jupyter');
	$: showOverviewTab = hasMessages;
	// Mologate: once the chat has had tasks (it stays after Clear empties the list).
	$: showTasksTab = isSavedChatId(chatId) && $chatTasks.chatId === chatId && $chatTasks.visible;

	// Tab fallback: if active tab becomes hidden, switch to another one
	$: if (!showOverviewTab && $chatControlsTab === 'overview' && showFilesTab)
		$chatControlsTab = 'files';
	$: if (!showFilesTab && $chatControlsTab === 'files') $chatControlsTab = 'overview';
	$: if (!showTasksTab && $chatControlsTab === 'tasks')
		$chatControlsTab = showFilesTab ? 'files' : 'overview';
	$: if (!showOverviewTab && !showFilesTab && showTasksTab && $chatControlsTab === 'overview')
		$chatControlsTab = 'tasks';

	// Auto-close if there is nothing to show
	$: if (!showFilesTab && !showOverviewTab && !showTasksTab && !specialPanel) {
		showControls.set(false);
	}

	// Auto-switch to Files tab when display_file is triggered
	$: if ($showFileNavPath && terminalFilesAvailable) {
		$chatControlsTab = 'files';
		showControls.set(true);
	}

	// Keep Files selected when a terminal is active; opening the panel is handled by selection UI.
	$: if ($selectedTerminalId && terminalFilesAvailable) {
		$chatControlsTab = 'files';
	}

	// Mologate: the Tasks tab, opened from the header, a message row or a tool call, on one task
	// if the request names it.
	let selectedTaskId: string | null = null;
	let tasksChatId = chatId;
	$: if (chatId !== tasksChatId) {
		tasksChatId = chatId;
		selectedTaskId = null;
	}
	$: if ($showTasks) {
		if ($showTasks.taskId !== undefined) selectedTaskId = $showTasks.taskId ?? null;
		$chatControlsTab = 'tasks';
		showArtifacts.set(false);
		showEmbeds.set(false);
		showControls.set(true);
		showTasks.set(null);
	}

	// Clear selected direct terminal if user lost permission
	$: if (
		$selectedTerminalId &&
		$terminalServers !== null &&
		!($terminalServers ?? []).some((t) => t.id && t.id === $selectedTerminalId) &&
		!($user?.role === 'admin' || ($user?.permissions?.features?.direct_tool_servers ?? true))
	) {
		selectedTerminalId.set(null);
	}

	const handleMediaQuery = (e) => {
		largeScreen = e.matches;
	};

	const onMouseDown = () => {
		dragged = true;
	};
	const onMouseUp = () => {
		dragged = false;
	};

	onMount(() => {
		const mediaQuery = window.matchMedia('(min-width: 1024px)');
		mediaQuery.addEventListener('change', handleMediaQuery);
		handleMediaQuery(mediaQuery);

		let isDestroyed = false;

		const init = async () => {
			await tick();

			if (isDestroyed) return;

			setTimeout(() => {
				mounted = true;
			}, 0);
		};
		init();

		document.addEventListener('mousedown', onMouseDown);
		document.addEventListener('mouseup', onMouseUp);

		return () => {
			isDestroyed = true;
			mounted = false;
			if (!largeScreen) {
				showControls.set(false);
			}
			mediaQuery.removeEventListener('change', handleMediaQuery);
			document.removeEventListener('mousedown', onMouseDown);
			document.removeEventListener('mouseup', onMouseUp);
		};
	});

	const closeHandler = () => {
		if (!largeScreen) {
			showControls.set(false);
		}
		showArtifacts.set(false);
		showEmbeds.set(false);
	};

	$: if (mounted && !chatId) closeHandler();

	// Helper: is a "special" full-screen panel active?
	$: specialPanel = $showArtifacts || $showEmbeds;
</script>

{#if !largeScreen}
	{#if $showControls}
		<Drawer
			show={$showControls}
			onClose={() => showControls.set(false)}
			className="min-h-[100dvh] !bg-white dark:!bg-gray-850"
		>
			<div class="h-[100dvh] flex flex-col">
				{#if $showEmbeds}
					<Embeds />
				{:else if $showArtifacts}
					<Artifacts {history} />
				{:else}
					<!-- Files / Overview tabs -->
					<div class="flex flex-col h-full min-h-0">
						<!-- Tab bar -->
						<div class="flex items-center justify-between px-2 pt-2 pb-2 shrink-0">
							<div class="flex gap-1 min-w-0 overflow-x-auto scrollbar-hidden">
								{#if showFilesTab}
									<button
										class="px-2.5 py-1 text-sm rounded-lg transition whitespace-nowrap {$chatControlsTab ===
										'files'
											? 'bg-gray-100/40 dark:bg-gray-800/25 font-normal text-gray-700 dark:text-gray-200'
											: 'text-gray-500 dark:text-gray-400 hover:bg-gray-100/30 dark:hover:bg-gray-800/20 hover:text-gray-600 dark:hover:text-gray-300'}"
										on:click={() => ($chatControlsTab = 'files')}
									>
										{$i18n.t('Files')}
									</button>
								{/if}
								{#if showTasksTab}
									<button
										class="px-2.5 py-1 text-sm rounded-lg transition whitespace-nowrap {$chatControlsTab ===
										'tasks'
											? 'bg-gray-100/40 dark:bg-gray-800/25 font-normal text-gray-700 dark:text-gray-200'
											: 'text-gray-500 dark:text-gray-400 hover:bg-gray-100/30 dark:hover:bg-gray-800/20 hover:text-gray-600 dark:hover:text-gray-300'}"
										on:click={() => ($chatControlsTab = 'tasks')}
									>
										{$i18n.t('Tasks')}
										{#if $chatTasks.running}
											<span class="ml-0.5 text-xs tabular-nums text-gray-400 dark:text-gray-500"
												>{$chatTasks.running}</span
											>
										{/if}
									</button>
								{/if}
								{#if showOverviewTab}
									<button
										class="px-2.5 py-1 text-sm rounded-lg transition whitespace-nowrap {$chatControlsTab ===
										'overview'
											? 'bg-gray-100/40 dark:bg-gray-800/25 font-normal text-gray-700 dark:text-gray-200'
											: 'text-gray-500 dark:text-gray-400 hover:bg-gray-100/30 dark:hover:bg-gray-800/20 hover:text-gray-600 dark:hover:text-gray-300'}"
										on:click={() => ($chatControlsTab = 'overview')}
									>
										{$i18n.t('Overview')}
									</button>
								{/if}
							</div>
							<button
								class="p-1 rounded-lg text-gray-500 dark:text-gray-400"
								on:click={() => showControls.set(false)}
								aria-label={$i18n.t('Close')}
							>
								<svg
									xmlns="http://www.w3.org/2000/svg"
									viewBox="0 0 24 24"
									fill="none"
									stroke="currentColor"
									stroke-width="1.5"
									class="size-4"
								>
									<path stroke-linecap="round" stroke-linejoin="round" d="M6 18 18 6M6 6l12 12" />
								</svg>
							</button>
						</div>

						<div
							class="flex-1 min-h-0 {$chatControlsTab === 'overview' || $chatControlsTab === 'tasks'
								? 'h-full'
								: ''}"
						>
							{#if $chatControlsTab === 'tasks' && showTasksTab}
								<TasksPanel
									chatId={chatId ?? ''}
									{history}
									{chatUser}
									showMessage={(message: any, scroll?: boolean) => {
										showControls.set(false);
										showMessage(message, scroll);
									}}
									bind:selectedTaskId
								/>
							{:else if $chatControlsTab === 'overview'}
								<Overview
									{history}
									{chatUser}
									onNodeClick={(e) => {
										const node = e.node;
										showMessage(node.data.message, true);
									}}
								/>
							{:else if $chatControlsTab === 'files' && terminalFilesAvailable && $selectedTerminalId}
								<FileNav {chatId} />
							{:else if $chatControlsTab === 'files' && codeInterpreterEnabled}
								<PyodideFileNav />
							{/if}
						</div>
					</div>
				{/if}
			</div>
		</Drawer>
	{/if}
{:else}
	<ResizableSidePanel
		open={$showControls}
		bind:width={controlsWidth}
		minWidth={350}
		minSiblingWidth={360}
		closeOnDragBelowMinWidth
		onClose={() => showControls.set(false)}
		storageKey="chatControlsSize"
		className="h-full z-10 bg-white dark:bg-gray-900"
	>
		<div class="flex h-full max-h-full min-h-full">
			<div
				class="w-full {specialPanel
					? ' '
					: 'bg-white dark:bg-gray-900'} z-40 pointer-events-auto {$chatControlsTab === 'files' ||
				$chatControlsTab === 'tasks'
					? ''
					: 'overflow-y-auto'} scrollbar-hidden"
				id="controls-container"
			>
				{#if $showEmbeds}
					<Embeds overlay={dragged} />
				{:else if $showArtifacts}
					<Artifacts {history} overlay={dragged} />
				{:else}
					<!-- Files / Overview tabs -->
					<div class="flex flex-col h-full min-h-0">
						<!-- Tab bar -->
						<div class="flex items-center justify-between px-2 pt-2 pb-2 shrink-0">
							<div class="flex gap-1 min-w-0 overflow-x-auto scrollbar-hidden">
								{#if showFilesTab}
									<button
										class="px-2.5 py-1 text-sm rounded-lg transition whitespace-nowrap {$chatControlsTab ===
										'files'
											? 'bg-gray-100/40 dark:bg-gray-800/25 font-normal text-gray-700 dark:text-gray-200'
											: 'text-gray-500 dark:text-gray-400 hover:bg-gray-100/30 dark:hover:bg-gray-800/20 hover:text-gray-600 dark:hover:text-gray-300'}"
										on:click={() => ($chatControlsTab = 'files')}
									>
										{$i18n.t('Files')}
									</button>
								{/if}
								{#if showTasksTab}
									<button
										class="px-2.5 py-1 text-sm rounded-lg transition whitespace-nowrap {$chatControlsTab ===
										'tasks'
											? 'bg-gray-100/40 dark:bg-gray-800/25 font-normal text-gray-700 dark:text-gray-200'
											: 'text-gray-500 dark:text-gray-400 hover:bg-gray-100/30 dark:hover:bg-gray-800/20 hover:text-gray-600 dark:hover:text-gray-300'}"
										on:click={() => ($chatControlsTab = 'tasks')}
									>
										{$i18n.t('Tasks')}
										{#if $chatTasks.running}
											<span class="ml-0.5 text-xs tabular-nums text-gray-400 dark:text-gray-500"
												>{$chatTasks.running}</span
											>
										{/if}
									</button>
								{/if}
								{#if showOverviewTab}
									<button
										class="px-2.5 py-1 text-sm rounded-lg transition whitespace-nowrap {$chatControlsTab ===
										'overview'
											? 'bg-gray-100/40 dark:bg-gray-800/25 font-normal text-gray-700 dark:text-gray-200'
											: 'text-gray-500 dark:text-gray-400 hover:bg-gray-100/30 dark:hover:bg-gray-800/20 hover:text-gray-600 dark:hover:text-gray-300'}"
										on:click={() => ($chatControlsTab = 'overview')}
									>
										{$i18n.t('Overview')}
									</button>
								{/if}
							</div>
							<button
								class="p-1 rounded-lg text-gray-500 dark:text-gray-400"
								on:click={() => showControls.set(false)}
								aria-label={$i18n.t('Close')}
							>
								<svg
									xmlns="http://www.w3.org/2000/svg"
									viewBox="0 0 24 24"
									fill="none"
									stroke="currentColor"
									stroke-width="1.5"
									class="size-4"
								>
									<path stroke-linecap="round" stroke-linejoin="round" d="M6 18 18 6M6 6l12 12" />
								</svg>
							</button>
						</div>

						<div
							class="flex-1 min-h-0 {$chatControlsTab === 'overview' || $chatControlsTab === 'tasks'
								? 'h-full'
								: ''}"
						>
							{#if $chatControlsTab === 'tasks' && showTasksTab}
								<TasksPanel
									chatId={chatId ?? ''}
									{history}
									{chatUser}
									{showMessage}
									bind:selectedTaskId
								/>
							{:else if $chatControlsTab === 'overview'}
								<Overview
									{history}
									{chatUser}
									onNodeClick={(e) => {
										const node = e.node;
										if (node?.data?.message?.favorite) {
											history.messages[node.data.message.id].favorite = true;
										} else {
											history.messages[node.data.message.id].favorite = null;
										}
										showMessage(node.data.message, true);
									}}
								/>
							{:else if $chatControlsTab === 'files' && terminalFilesAvailable && $selectedTerminalId}
								<FileNav overlay={dragged} {chatId} />
							{:else if $chatControlsTab === 'files' && codeInterpreterEnabled}
								<PyodideFileNav overlay={dragged} />
							{/if}
						</div>
					</div>
				{/if}
			</div>
		</div>
	</ResizableSidePanel>
{/if}
