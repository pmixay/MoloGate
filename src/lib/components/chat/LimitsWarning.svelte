<script lang="ts">
	// Mologate: the low-limit warning, above the message box. When the selected model's 5-hour,
	// weekly or credit limit runs low across every account that can serve it, it says how much is
	// left, about how many minutes of work that is at the recent pace, and when it resets
	// (utils/provider_limits.py, `outlook`). The context indicator loads the limits; this reads
	// them from the same store. A dismissed warning comes back when it gets
	// worse or the window resets. Mologate, phase 4: it also warns from 80% of the member's own
	// share of the team pool, and says when their replies use a worker model.
	import { getContext } from 'svelte';
	import { slide } from 'svelte/transition';

	import { modelLimits } from '$lib/stores';
	import {
		lowWindows,
		observing,
		shareWarnings,
		shareWarningText,
		stillDismissed,
		warningText,
		type DismissedWarning,
		type Outlook
	} from '$lib/utils/providerLimits';

	import XMark from '$lib/components/icons/XMark.svelte';

	const i18n: any = getContext('i18n');

	export let selectedModels: string[] = [];

	const STORAGE_KEY = 'mologate-dismissed-limit-warnings';

	// Per model and window. Browser storage can be unavailable; the warning still shows then.
	const readDismissed = (): Record<string, DismissedWarning> => {
		try {
			const value = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}');
			return value && typeof value === 'object' ? value : {};
		} catch {
			return {};
		}
	};

	let dismissed = readDismissed();

	$: modelId = selectedModels?.[0] ?? '';
	$: t = (key: string, values?: Record<string, string | number>) => $i18n.t(key, values);
	$: outlook = $modelLimits.modelId === modelId ? ($modelLimits.limits?.outlook ?? []) : [];
	// Mologate: the member's own share of the team pool warns too, from 80% of it (phase 4).
	$: allowance = $modelLimits.modelId === modelId ? ($modelLimits.limits?.allowance ?? null) : null;
	// When no shared account has room for the member, that is the warning, whatever their share says.
	$: poolEmpty =
		allowance && allowance.mode !== 'off' && allowance.tier === 'empty'
			? [
					{
						id: 'pool-empty',
						level: (observing(allowance) ? 'low' : 'critical') as Outlook['level'],
						resets_at: null,
						text: observing(allowance)
							? t(
									'By the fair-use rules the team pool has no room for this model. Limits are not enforced yet, so your requests still go through.'
								)
							: t(
									'The team pool has no room for this model right now. A model from another provider may still have some.'
								)
					}
				]
			: [];
	$: shown = [
		...poolEmpty,
		...(poolEmpty.length ? [] : shareWarnings(allowance)).map((item) => ({
			id: item.id,
			// Mologate: in observe nothing is refused or swapped, so nothing is critical.
			level: (item.level === 'empty' && !observing(allowance)
				? 'critical'
				: 'low') as Outlook['level'],
			resets_at: item.resets_at,
			text: shareWarningText(item, allowance!, t)
		})),
		...lowWindows(outlook).map((item) => ({
			id: item.id,
			level: item.level,
			resets_at: item.resets_at,
			text: warningText(item, t)
		}))
	].filter((item) => !stillDismissed(item, dismissed[`${modelId}:${item.id}`]));

	const dismiss = (item: { id: string; level: Outlook['level']; resets_at: number | null }) => {
		const now = Date.now() / 1000;
		dismissed = Object.fromEntries(
			Object.entries({
				...dismissed,
				[`${modelId}:${item.id}`]: { level: item.level, resets_at: item.resets_at }
			}).filter(([, value]) => !value.resets_at || value.resets_at > now)
		);
		try {
			localStorage.setItem(STORAGE_KEY, JSON.stringify(dismissed));
		} catch {
			// Hidden for this page only.
		}
	};
</script>

{#if shown.length}
	<div class="mx-1 flex flex-col gap-1" transition:slide={{ duration: 150 }}>
		{#each shown as item (item.id)}
			<div
				role={item.level === 'low' ? 'status' : 'alert'}
				class="flex items-start gap-2 rounded-xl border px-3 py-2 text-left text-xs {item.level ===
				'low'
					? 'border-amber-200 bg-amber-50 text-amber-900 dark:border-amber-500/30 dark:bg-amber-400/10 dark:text-amber-200'
					: 'border-red-200 bg-red-50 text-red-900 dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-200'}"
			>
				<svg
					viewBox="0 0 20 20"
					fill="currentColor"
					class="mt-px size-3.5 shrink-0"
					aria-hidden="true"
				>
					<path
						fill-rule="evenodd"
						d="M8.485 2.495c.673-1.167 2.357-1.167 3.03 0l6.28 10.875c.673 1.167-.17 2.625-1.516 2.625H3.72c-1.347 0-2.189-1.458-1.515-2.625L8.485 2.495ZM10 5a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5A.75.75 0 0 1 10 5Zm0 9a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z"
						clip-rule="evenodd"
					/>
				</svg>
				<span class="min-w-0 flex-1 leading-4">{item.text}</span>
				<button
					type="button"
					class="-m-1 shrink-0 rounded-md p-1 opacity-70 transition hover:bg-black/5 hover:opacity-100 dark:hover:bg-white/10"
					aria-label={$i18n.t('Dismiss')}
					on:click={() => dismiss(item)}
				>
					<XMark className="size-3" strokeWidth="2" />
				</button>
			</div>
		{/each}
	</div>
{/if}
