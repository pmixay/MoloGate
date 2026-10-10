<script lang="ts">
	import { copyToClipboard, unescapeHtml } from '$lib/utils';
	import { toast } from 'svelte-sonner';

	import { getContext } from 'svelte';

	import { settings } from '$lib/stores';

	const i18n = getContext('i18n');

	export let token;
	export let done = true;
</script>

<!-- svelte-ignore a11y-click-events-have-key-events -->
<!-- svelte-ignore a11y-no-noninteractive-element-interactions -->
<code
	class="codespan cursor-pointer {!done && ($settings?.chatFadeStreamingText ?? true)
		? 'fade-in-token'
		: ''}"
	on:click={() => {
		copyToClipboard(unescapeHtml(token.text));
		toast.success($i18n.t('Copied to clipboard'));
	}}>{unescapeHtml(token.text)}</code
>

<style>
	/* Nocturne inline code: Geist Mono 13px on a hairline chip (overrides the global .codespan) */
	.codespan {
		font-size: 0.8667em;
		font-weight: 400;
		padding: 0.0625rem 0.3125rem;
		margin: 0 0.0625rem;
		border-radius: 5px;
		border: 1px solid var(--color-gray-200, #dfe1ea);
		background-color: var(--color-gray-100, #eceef5);
		color: var(--color-gray-900, #0f1016);
		transition: border-color 120ms;
	}
	.codespan:hover {
		border-color: var(--color-gray-300, #c8cad8);
	}
	:global(.dark) .codespan {
		border-color: rgb(58 60 76 / 0.6);
		background-color: rgb(35 36 47 / 0.6);
		color: var(--color-gray-100, #eceef5);
	}
	:global(.dark) .codespan:hover {
		border-color: var(--color-gray-700, #3a3c4c);
	}
</style>
