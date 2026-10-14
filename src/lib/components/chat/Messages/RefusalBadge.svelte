<script lang="ts">
	// Mologate: a warning in the answer's action row when the model declined the request
	// (backend/open_webui/utils/refusals.py). The tooltip says what was declined and how it was told.
	import { getContext } from 'svelte';

	import Tooltip from '$lib/components/common/Tooltip.svelte';

	const i18n: any = getContext('i18n');

	export let refusal: {
		source: 'provider' | 'wording';
		kind: 'refusal' | 'filter';
		partial?: boolean;
		quote?: string | null;
	};

	const escape = (text: string) =>
		text.replace(
			/[&<>"']/g,
			(c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c] ?? c
		);

	$: label =
		refusal.kind === 'filter'
			? $i18n.t('Filtered')
			: refusal.partial
				? $i18n.t('Partly declined')
				: $i18n.t('Declined');

	$: summary =
		refusal.kind === 'filter'
			? refusal.partial
				? $i18n.t("The provider's content filter stopped this answer partway.")
				: $i18n.t("The provider's content filter blocked this answer.")
			: refusal.partial
				? $i18n.t('The model declined part of this request and answered the rest.')
				: $i18n.t('The model declined this request.');

	$: how =
		refusal.source === 'provider'
			? $i18n.t('Reported by the provider.')
			: $i18n.t("Detected from the answer's wording, so it may be wrong.");

	$: tooltip = [
		`<div class="font-medium">${escape(summary)}</div>`,
		refusal.quote ? `<div class="mt-1 italic opacity-90">“${escape(refusal.quote)}”</div>` : '',
		`<div class="mt-1 opacity-70">${escape(how)} ${escape(
			$i18n.t('Rephrasing the request or trying another model may help.')
		)}</div>`
	].join('');
</script>

<Tooltip content={tooltip} placement="bottom" className="flex">
	<div
		role="status"
		aria-label={`${label}. ${summary}`}
		data-refusal={refusal.kind}
		class="ms-1 inline-flex h-6 select-none items-center gap-1 rounded-md border border-amber-200 bg-amber-50 px-1.5 text-xs font-medium text-amber-700 dark:border-amber-500/30 dark:bg-amber-400/10 dark:text-amber-300"
	>
		<svg
			class="size-3.5 shrink-0"
			viewBox="0 0 24 24"
			fill="none"
			stroke="currentColor"
			stroke-width="2"
			stroke-linecap="round"
			stroke-linejoin="round"
			aria-hidden="true"
		>
			<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" />
			<path d="M12 9v4" />
			<path d="M12 17h.01" />
		</svg>
		<span>{label}</span>
	</div>
</Tooltip>
