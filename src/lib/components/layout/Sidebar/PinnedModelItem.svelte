<script lang="ts">
	import { getContext } from 'svelte';

	const i18n = getContext('i18n');

	import { WEBUI_API_BASE_URL, WEBUI_BASE_URL } from '$lib/constants';
	import { resolveLocalizedModelName } from '$lib/utils/localizedContent';

	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import PinSlash from './icons/PinSlash.svelte';

	export let model = null;
	export let shiftKey = false;
	export let onClick = () => {};
	export let onUnpin = () => {};

	let mouseOver = false;
	$: localizedModelName = resolveLocalizedModelName(model, $i18n.language);
</script>

{#if model}
	<!-- svelte-ignore a11y-no-static-element-interactions -->
	<div
		class=" flex justify-center text-gray-700 dark:text-gray-400 cursor-grab relative group"
		data-id={model?.id}
		on:mouseenter={(e) => {
			mouseOver = true;
		}}
		on:mouseleave={(e) => {
			mouseOver = false;
		}}
	>
		<a
			class="grow flex h-7.5 min-w-0 items-center gap-2.5 rounded-[7px] pl-[0.6875rem] pr-2 group-hover:bg-gray-100 group-hover:text-gray-900 dark:group-hover:bg-gray-900 dark:group-hover:text-gray-100 transition outline-none focus-visible:ring-2 focus-visible:ring-accent"
			href="/?model={model?.id}"
			on:click={onClick}
			draggable="false"
		>
			<div class="self-center shrink-0">
				<img
					src={`${WEBUI_API_BASE_URL}/models/model/profile/image?id=${model.id}&lang=${$i18n.language}`}
					class=" size-[1.125rem] rounded-[5px] object-cover"
					alt="logo"
					on:error={(e) => {
						// LICENSE covers this Open WebUI fallback logo.
						// Do not alter, remove, obscure, or replace it except as LICENSE permits:
						// https://docs.openwebui.com/license.
						e.currentTarget.src = '/favicon.png';
					}}
				/>
			</div>

			<div class="flex min-w-0 self-center">
				<div class=" self-center truncate font-mono text-[0.78125rem] leading-5">
					{localizedModelName}
				</div>
			</div>
		</a>

		{#if mouseOver && shiftKey && onUnpin}
			<div class="absolute right-2.5 inset-y-0 flex items-center">
				<div class=" flex items-center self-center space-x-1.5">
					<Tooltip content={$i18n.t('Unpin')} className="flex items-center">
						<button
							class=" self-center text-gray-500 hover:text-gray-900 dark:hover:text-gray-100 transition"
							on:click={() => {
								onUnpin();
							}}
							type="button"
						>
							<PinSlash className="size-3.5" strokeWidth="1.5" />
						</button>
					</Tooltip>
				</div>
			</div>
		{/if}
	</div>
{/if}
