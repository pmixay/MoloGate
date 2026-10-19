<script lang="ts">
	// Mologate: the "⋯" (Extra) menu next to "+": settings of this chat's own container. For now its
	// internet switch, which works mid-chat too (docs/container-nodes.md).
	import { getContext } from 'svelte';
	import { toast } from 'svelte-sonner';

	import { newChatContainerNetwork } from '$lib/stores';
	import { isSavedChatId } from '$lib/utils/chatId';
	import {
		getChatContainer,
		getContainerLimits,
		setChatContainerNetwork,
		type ChatContainer,
		type ContainerLimits
	} from '$lib/apis/containers';

	import Dropdown from '$lib/components/common/Dropdown.svelte';
	import DropdownMenu from '$lib/components/common/DropdownMenu.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import EllipsisHorizontal from '$lib/components/icons/EllipsisHorizontal.svelte';
	import GlobeAlt from '$lib/components/icons/GlobeAlt.svelte';
	import Cube from '$lib/components/icons/Cube.svelte';
	import GitHubConnection from './GitHubConnection.svelte';
	import Github from '$lib/components/icons/Github.svelte';
	import type { ChatGitHubConnection } from '$lib/apis/containers';
	import InfoCircle from '$lib/components/icons/InfoCircle.svelte';

	const i18n: any = getContext('i18n');

	export let chatId = '';

	let show = false;
	let github: GitHubConnection;
	let githubConnection: ChatGitHubConnection | null = null;
	let githubBusy = false;
	let githubError = '';
	let githubChatId = chatId;
	$: if (githubChatId !== chatId) {
		githubChatId = chatId;
		githubConnection = null;
		githubBusy = false;
		githubError = '';
	}
	const openGitHub = () => {
		show = false;
		github?.open();
	};
	let container: ChatContainer | null = null;
	let saving = false;
	// Re-mounts the switch when a change is refused, so it shows the real state again.
	let switchKey = 0;

	$: saved = isSavedChatId(chatId);
	// A chat that was just created may not have its container yet; until then the switch shows the
	// choice made before sending, which Chat.svelte hands to the container.
	$: network =
		$newChatContainerNetwork !== null && !container?.exists
			? $newChatContainerNetwork
			: (container?.network ?? true);

	let loadedFor: string | null = null;
	$: if (chatId !== loadedFor) {
		loadedFor = chatId;
		container = null;
		saving = false;
		load();
	}
	$: if (show || $newChatContainerNetwork === null) {
		load();
	}

	const load = async () => {
		if (!isSavedChatId(chatId)) {
			container = null;
			return;
		}
		const id = chatId;
		try {
			const data = await getChatContainer(localStorage.token, id);
			if (id === chatId) container = data;
		} catch {
			if (id === chatId) container = null;
		}
	};

	const setNetwork = async (enabled: boolean) => {
		if (!saved) {
			newChatContainerNetwork.set(enabled);
			return;
		}
		const id = chatId;
		saving = true;
		try {
			const data = await setChatContainerNetwork(localStorage.token, id, enabled);
			if (id === chatId) container = data;
		} catch (error) {
			if (id !== chatId) return;
			toast.error(`${error}`);
			switchKey += 1;
		} finally {
			if (id === chatId) saving = false;
		}
	};

	const formatBytes = (bytes: number) => {
		const units = ['B', 'KB', 'MB', 'GB', 'TB'];
		let value = bytes;
		let unit = 0;
		while (value >= 1024 && unit < units.length - 1) {
			value /= 1024;
			unit += 1;
		}
		return `${Number(value.toFixed(1))} ${units[unit]}`;
	};

	$: stateLabel = !saved
		? $i18n.t('Starts when you send')
		: ({
				running: $i18n.t('Running'),
				paused: $i18n.t('Paused'),
				exited: $i18n.t('Stopped'),
				created: $i18n.t('Stopped'),
				unreachable: $i18n.t('Node unreachable')
			}[container?.state ?? 'none'] ?? $i18n.t('Not started'));

	$: stateColor =
		container?.state === 'running'
			? 'bg-green-500'
			: container?.state === 'paused'
				? 'bg-amber-400'
				: container?.state === 'unreachable'
					? 'bg-red-500'
					: 'bg-gray-400 dark:bg-gray-600';

	// What the "i" button shows: the container's size, the same on every node.
	let defaultLimits: ContainerLimits | null = null;
	$: if (show && !container?.limits && !defaultLimits) {
		getContainerLimits(localStorage.token)
			.then((data) => (defaultLimits = data))
			.catch(() => {});
	}
	$: shownLimits = container?.limits ?? defaultLimits;
	$: limits = shownLimits
		? [
				`${shownLimits.cpus} CPU`,
				`${formatBytes(shownLimits.memory)} RAM`,
				...(shownLimits.disk ? [`${formatBytes(shownLimits.disk)} ${$i18n.t('disk')}`] : [])
			].join(' · ')
		: '';
</script>

<Dropdown bind:show visualViewportAware>
	<Tooltip content={network ? $i18n.t('Extra') : $i18n.t('Extra · Network off')}>
		<button
			type="button"
			id="extra-menu-button"
			class="relative bg-transparent border border-gray-200 text-gray-600 hover:bg-gray-100 hover:text-gray-900 dark:border-gray-800 dark:text-gray-400 dark:hover:bg-gray-800/60 dark:hover:text-gray-100 rounded-[9px] size-[34px] flex justify-center items-center transition outline-hidden focus-visible:ring-2 focus-visible:ring-gray-900 dark:focus-visible:ring-accent shrink-0"
			aria-label={$i18n.t('Extra')}
		>
			<EllipsisHorizontal className="size-[18px]" strokeWidth="1.75" />
			{#if !network}
				<span
					class="absolute -top-0.5 -right-0.5 size-2 rounded-full bg-amber-400 ring-2 ring-white dark:ring-gray-850"
					aria-hidden="true"
				></span>
			{/if}
		</button>
	</Tooltip>

	<div slot="content">
		<DropdownMenu className="w-72 p-1!">
			<div
				class="flex items-center gap-2 px-2 pt-1.5 pb-1 text-xs text-gray-500 dark:text-gray-400"
			>
				<Cube className="size-3.5 shrink-0" strokeWidth="1.75" />
				<span class="shrink-0 whitespace-nowrap font-medium text-gray-700 dark:text-gray-200"
					>{$i18n.t('Chat container')}</span
				>
				{#if limits}
					<Tooltip content={limits} placement="top">
						<button
							type="button"
							class="-m-0.5 flex shrink-0 rounded-full p-0.5 text-gray-400 hover:text-gray-700 dark:text-gray-500 dark:hover:text-gray-200"
							aria-label={$i18n.t('Container resources: {{limits}}', { limits })}
						>
							<InfoCircle className="size-3.5" strokeWidth="1.75" />
						</button>
					</Tooltip>
				{/if}
				<span class="ml-auto flex items-center gap-1.5 min-w-0">
					<span class="size-1.5 rounded-full shrink-0 {stateColor}" aria-hidden="true"></span>
					<span class="truncate">{stateLabel}</span>
				</span>
			</div>

			<div
				class="flex items-center gap-2.5 px-2 py-2 rounded-xl hover:bg-gray-50/60 dark:hover:bg-gray-800/60 {saving
					? 'opacity-60'
					: ''}"
			>
				<GlobeAlt className="size-4 shrink-0" strokeWidth="1.6" />
				<div class="flex-1 min-w-0">
					<div class="text-[0.8125rem]" id="container-network-label">
						{$i18n.t('Network access')}
					</div>
					<div class="text-xs text-gray-500 dark:text-gray-400 leading-snug">
						{network
							? $i18n.t("Full internet for commands in this chat's container")
							: $i18n.t("Commands in this chat's container can't reach the internet")}
					</div>
				</div>
				{#key switchKey}
					<Switch
						state={network}
						ariaLabelledbyId="container-network-label"
						on:change={(e) => setNetwork(e.detail)}
					/>
				{/key}
			</div>
			<button
				type="button"
				class="flex w-full items-center gap-2.5 px-2 py-2 rounded-xl text-left hover:bg-gray-50/60 dark:hover:bg-gray-800/60 disabled:opacity-60"
				disabled={githubBusy}
				on:click={openGitHub}
				aria-busy={githubBusy}
			>
				<Github className="size-4 shrink-0" />
				<div class="flex-1 min-w-0">
					<div class="text-[0.8125rem]">
						{githubConnection?.connected ? $i18n.t('Manage GitHub') : $i18n.t('Connect GitHub')}
					</div>
					<div class="text-xs text-gray-500 dark:text-gray-400 leading-snug" role="status">
						{#if !saved}
							{$i18n.t('Send a message first to connect GitHub')}
						{:else if githubBusy}
							{$i18n.t('Checking GitHub…')}
						{:else if githubError}
							{$i18n.t('Unable to check GitHub. Click to retry.')}
						{:else if githubConnection?.connected}
							{githubConnection.login
								? $i18n.t('Connected as {{login}}', { login: githubConnection.login })
								: $i18n.t('Connected')}
						{:else if !network}
							{$i18n.t('Enable network access to connect GitHub')}
						{:else if githubConnection?.pending}
							{$i18n.t('Waiting for authorization')}
						{:else}
							{$i18n.t('Authorize GitHub for this chat')}
						{/if}
					</div>
				</div>
			</button>
		</DropdownMenu>
	</div>
</Dropdown>

<!-- Kept outside dropdown content so device authorization survives closing the Extra menu. -->
{#key chatId}
	<GitHubConnection
		bind:this={github}
		{chatId}
		{network}
		menuOpen={show}
		bind:connection={githubConnection}
		bind:busy={githubBusy}
		bind:error={githubError}
	/>
{/key}
