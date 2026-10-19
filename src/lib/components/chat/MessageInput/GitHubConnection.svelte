<script lang="ts">
	import { getContext, onDestroy } from 'svelte';
	import { toast } from 'svelte-sonner';
	import {
		getChatGitHub,
		connectChatGitHub,
		pollChatGitHub,
		disconnectChatGitHub,
		type ChatGitHubConnection
	} from '$lib/apis/containers';
	import { isSavedChatId } from '$lib/utils/chatId';
	import Modal from '$lib/components/common/Modal.svelte';

	const i18n: any = getContext('i18n');
	export let chatId = '';
	export let network = true;
	export let menuOpen = false;
	export let connection: ChatGitHubConnection | null = null;
	export let busy = false;
	export let error = '';

	let show = false;
	let previousMenuOpen = false;
	let previousShow = false;
	let previousNetwork = network;
	let destroyed = false;
	let generation = 0;
	let controller: AbortController | null = null;
	let timer: ReturnType<typeof setTimeout> | undefined;
	let deadline = 0;

	// This component is keyed by chatId in ExtraMenu: a different chat never inherits a request.
	const cancel = () => {
		generation += 1;
		controller?.abort();
		controller = null;
		clearTimeout(timer);
		timer = undefined;
		busy = false;
	};

	const schedulePoll = () => {
		clearTimeout(timer);
		if (!show || !network || !connection?.pending || error || destroyed) return;
		if (!deadline) deadline = Date.now() + 15 * 60 * 1000;
		if (Date.now() >= deadline) {
			error = $i18n.t('Authorization timed out. Try connecting again.');
			connection = { connected: false };
			return;
		}
		timer = setTimeout(() => request('poll'), 3000);
	};

	type Action = 'status' | 'connect' | 'poll' | 'disconnect';
	const request = async (action: Action) => {
		if (!isSavedChatId(chatId) || destroyed) return;
		if ((action === 'connect' || action === 'poll') && !network) {
			error = $i18n.t('Enable network access in the Extra menu before connecting GitHub.');
			return;
		}
		cancel();
		const current = generation;
		const id = chatId;
		controller = new AbortController();
		busy = true;
		error = '';
		try {
			const api = {
				status: getChatGitHub,
				connect: connectChatGitHub,
				poll: pollChatGitHub,
				disconnect: disconnectChatGitHub
			}[action];
			const data = await api(localStorage.token, id, controller.signal);
			if (destroyed || current !== generation || id !== chatId) return;
			// Poll replies may omit the still-valid device code.
			connection = data.pending ? { ...connection, ...data } : data;
			if (data.connected || action === 'disconnect') deadline = 0;
			if ((action === 'connect' || action === 'poll') && !data.connected && !data.pending) {
				error = $i18n.t('Authorization expired or was declined. Try connecting again.');
			}
			if (action === 'poll' && data.connected) toast.success($i18n.t('GitHub connected'));
		} catch (cause) {
			if (destroyed || current !== generation || id !== chatId) return;
			error = cause instanceof Error ? cause.message : String(cause);
			toast.error(error);
		} finally {
			if (!destroyed && current === generation && id === chatId) {
				busy = false;
				controller = null;
				// Errors stop automatic polling. A retry is always an explicit user action.
				schedulePoll();
			}
		}
	};

	export const open = () => {
		show = true;
		if (!isSavedChatId(chatId) || !network || busy) return;
		if (connection?.connected) return;
		if (connection?.pending && !error) {
			schedulePoll();
		} else {
			deadline = 0;
			request('connect');
		}
	};

	const copyCode = async () => {
		try {
			await navigator.clipboard.writeText(connection?.code ?? '');
			toast.success($i18n.t('Code copied'));
		} catch {
			toast.error($i18n.t('Unable to copy. Select and copy the code manually.'));
		}
	};

	$: if (menuOpen !== previousMenuOpen) {
		previousMenuOpen = menuOpen;
		if (menuOpen && !show) request('status');
	}
	$: if (show !== previousShow) {
		previousShow = show;
		if (!show) cancel();
	}
	$: if (network !== previousNetwork) {
		previousNetwork = network;
		cancel();
		if (network && (show || menuOpen)) request('status');
	}
	onDestroy(() => {
		destroyed = true;
		cancel();
	});
</script>

<Modal bind:show size="sm">
	<section class="p-6 space-y-4" aria-label={$i18n.t('GitHub connection')} aria-busy={busy}>
		<div class="flex items-center justify-between gap-3">
			<h2 class="text-lg font-semibold">{$i18n.t('Connect GitHub')}</h2>
			<button
				type="button"
				class="rounded-lg px-3 py-1 hover:bg-gray-100 dark:hover:bg-gray-800"
				on:click={() => (show = false)}>{$i18n.t('Close')}</button
			>
		</div>
		<div role="status" aria-live="polite" class="text-sm space-y-3">
			{#if !isSavedChatId(chatId)}
				<p>{$i18n.t('Send a message first to connect GitHub to this chat.')}</p>
			{:else if connection?.connected}
				<p>
					{connection.login
						? $i18n.t('Connected as {{login}}', { login: connection.login })
						: $i18n.t('GitHub connected')}
				</p>
			{:else if !network}
				<p>{$i18n.t('Enable network access in the Extra menu before connecting GitHub.')}</p>
			{:else if connection?.pending && connection?.code}
				<p>
					{$i18n.t(
						'Enter this code on GitHub, then authorize access. Connecting here does not authorize GitHub automatically.'
					)}
				</p>
				<div class="flex items-center gap-3">
					<code
						class="select-all rounded-lg bg-gray-100 dark:bg-gray-800 px-3 py-2 text-lg font-semibold tracking-widest"
						>{connection.code}</code
					>
					<button
						type="button"
						class="rounded-lg border border-gray-200 dark:border-gray-700 px-3 py-2"
						on:click={copyCode}>{$i18n.t('Copy code')}</button
					>
				</div>
				<a
					class="inline-block underline"
					href="https://github.com/login/device"
					target="_blank"
					rel="noopener noreferrer">{$i18n.t('Open GitHub to authorize')} ↗</a
				>
				{#if !error}<p class="text-gray-500">{$i18n.t('Waiting for GitHub authorization…')}</p>{/if}
			{:else if busy || connection?.pending}
				<p>{$i18n.t('Contacting GitHub…')}</p>
			{:else}
				<p>{$i18n.t('GitHub is not connected to this chat.')}</p>
			{/if}
		</div>
		{#if error}<p role="alert" class="text-sm text-red-600 dark:text-red-400">{error}</p>{/if}
		{#if connection?.connected}
			<button
				type="button"
				class="rounded-lg bg-gray-900 text-white dark:bg-white dark:text-gray-900 px-4 py-2 disabled:opacity-50"
				disabled={busy}
				on:click={() => request('disconnect')}
				>{busy ? $i18n.t('Disconnecting…') : $i18n.t('Disconnect GitHub')}</button
			>
		{:else if network && isSavedChatId(chatId) && !busy && (error || !connection?.pending)}
			<button
				type="button"
				class="rounded-lg bg-gray-900 text-white dark:bg-white dark:text-gray-900 px-4 py-2"
				on:click={() => {
					deadline = 0;
					request('connect');
				}}>{error ? $i18n.t('Retry connection') : $i18n.t('Connect GitHub')}</button
			>
		{/if}
	</section>
</Modal>
