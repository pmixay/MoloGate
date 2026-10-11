<script lang="ts">
	// Mologate: a message one agent of this chat sent another (backend utils/agent_messages.py).
	import { getContext } from 'svelte';
	import type { Readable } from 'svelte/store';

	import ChevronDown from '$lib/components/icons/ChevronDown.svelte';

	const i18n =
		getContext<Readable<{ t: (value: string, options?: Record<string, unknown>) => string }>>(
			'i18n'
		);

	export let content = '';
	export let from = '';
	export let fromName = '';
	export let fromChatId = '';
	// The chat this row is shown in: no link to it.
	export let chatId = '';

	const HEADER = /^\[(Message|Reply) from ([^\]\n]+)\]\n?/;

	let expanded = false;

	$: headers = content.match(/^\[(?:Message|Reply) from [^\]\n]+\]$/gm) ?? [];
	$: header = content.match(HEADER);
	$: single = headers.length <= 1;
	$: senderName =
		from === 'main' ? $i18n.t('Main agent') : fromName || (single && header ? header[2] : '');
	$: label = !single
		? $i18n.t('Messages from other agents')
		: header?.[1] === 'Reply'
			? $i18n.t('Reply from {{name}}', { name: senderName })
			: $i18n.t('Message from {{name}}', { name: senderName });
	$: body = single && header ? content.slice(header[0].length) : content;
	$: summary = (() => {
		const line = body
			.split('\n')
			.map((value) => value.trim())
			.find((value) => value && !value.startsWith('['));
		if (!line) return '';
		return line.length > 96 ? `${line.slice(0, 96)}...` : line;
	})();
	$: linkChatId = single && fromChatId && fromChatId !== chatId ? fromChatId : '';
</script>

<div class="w-full min-w-0 pb-1 agent-message-row">
	<div class="flex w-full min-w-0 items-center gap-2">
		<button
			type="button"
			class="flex min-w-0 items-center gap-2 text-left text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 transition-colors"
			aria-expanded={expanded}
			on:click={() => (expanded = !expanded)}
		>
			<span class="text-[0.75rem] font-medium shrink-0">{label}</span>
			{#if summary}
				<span class="text-[0.75rem] truncate min-w-0">{summary}</span>
			{/if}
			<ChevronDown
				className="size-3 text-gray-400 dark:text-gray-600 shrink-0 transition-transform duration-150 {expanded
					? 'rotate-180'
					: ''}"
			/>
		</button>
		{#if linkChatId}
			<a
				href={`/c/${linkChatId}`}
				class="ml-auto shrink-0 text-[0.6875rem] text-gray-400 dark:text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 hover:underline transition-colors"
			>
				{$i18n.t('Open conversation')}
			</a>
		{/if}
	</div>
	{#if expanded}
		<div
			class="mt-2 ml-3 border-l border-gray-100 dark:border-white/8 pl-3 text-[0.78125rem] leading-relaxed text-gray-600 dark:text-gray-400 whitespace-pre-wrap break-words"
		>
			{body}
		</div>
	{/if}
</div>
