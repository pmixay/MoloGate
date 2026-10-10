<script lang="ts">
	// Mologate: the answers to a permission prompt. "Always allow in this chat" approves this call
	// and stops prompts for the rest of the chat (backend utils/tool_permissions.py).
	import { getContext } from 'svelte';

	const i18n: any = getContext('i18n');

	export let disabled = false;
	export let onResolve: (approved: boolean, always?: boolean) => void = () => {};
</script>

<span class="flex shrink-0 flex-wrap justify-end gap-1">
	<button
		type="button"
		class="tool-call-allow-button text-[0.6875rem] px-2.5 py-0.5 rounded-md text-gray-600 dark:text-gray-300 bg-gray-100 dark:bg-white/8 hover:bg-gray-200 dark:hover:bg-white/12 transition-colors duration-100 disabled:opacity-50"
		{disabled}
		on:click|stopPropagation={() => onResolve(true)}
	>
		{$i18n.t('Allow')}
	</button>
	<button
		type="button"
		class="tool-call-always-allow-button text-[0.6875rem] px-2.5 py-0.5 rounded-md text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-white/8 transition-colors duration-100 disabled:opacity-50"
		{disabled}
		on:click|stopPropagation={() => onResolve(true, true)}
	>
		{$i18n.t('Always allow in this chat')}
	</button>
	<button
		type="button"
		class="tool-call-deny-button text-[0.6875rem] px-2 py-0.5 rounded-md text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 transition-colors duration-100 disabled:opacity-50"
		{disabled}
		on:click|stopPropagation={() => onResolve(false)}
	>
		{$i18n.t('Deny')}
	</button>
</span>
