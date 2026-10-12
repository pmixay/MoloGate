<script lang="ts">
	// Mologate: the selected model's usage limits as a ring next to the model selector. The outer
	// ring is the 5-hour window, the inner one the weekly window (or an OpenRouter key's credit
	// limit), added up across every account that can serve the model, since requests move on to
	// the next account when one runs out. When they run low, a label beside the ring says roughly
	// how many minutes of work are left at the recent pace (utils/provider_limits.py, `outlook`),
	// and LimitsWarning, above the message box, says it in full.
	import { getContext, onMount, tick } from 'svelte';
	import { autoUpdate, computePosition, flip, offset, shift, size } from '@floating-ui/dom';

	import { modelLimits, settings, showSettings } from '$lib/stores';
	import { getModelLimits } from '$lib/apis/models';
	import {
		formatPercent,
		lowWindows,
		outlookName,
		shortWarning,
		warningText,
		type Outlook
	} from '$lib/utils/providerLimits';
	import { flyAndScale } from '$lib/utils/transitions';

	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import ProviderLimitsList from './ProviderLimitsList.svelte';

	const i18n: any = getContext('i18n');

	export let selectedModels: string[] = [];
	export let history: { messages: Record<string, any>; currentId: string | null } = {
		messages: {},
		currentId: null
	};

	const REFRESH_MS = 60_000;
	const providers: Record<string, string> = {
		codex: 'ChatGPT',
		anthropic_api: 'Anthropic',
		openai_api: 'OpenAI',
		openrouter: 'OpenRouter',
		zai: 'Z.ai'
	};

	let show = false;
	let triggerElement: HTMLElement | null = null;
	let contentElement: HTMLElement | null = null;
	let loadedFor = '';
	let requestId = 0;

	$: modelId = selectedModels?.[0] ?? '';
	$: t = (key: string, values?: Record<string, string | number>) => $i18n.t(key, values);

	// The limits go to a store, so the low-limit warning above the message box shows them too.
	const load = async (id: string) => {
		const current = ++requestId;
		const result = id ? await getModelLimits(localStorage.token, id).catch(() => null) : null;
		// A newer request (another model, a later reply) wins.
		if (current === requestId) {
			modelLimits.set({ modelId: id, limits: result });
		}
	};

	$: if (modelId !== loadedFor) {
		modelLimits.set({ modelId, limits: null });
		loadedFor = modelId;
		load(modelId);
	}

	// Each reply moves the limits: read them again once the latest message is finished.
	$: replyKey = history?.currentId
		? `${history.currentId}:${history.messages?.[history.currentId]?.done ?? true}`
		: '';
	let lastReplyKey = '';
	$: if (replyKey !== lastReplyKey) {
		const finished = replyKey.endsWith(':true') && lastReplyKey !== '';
		lastReplyKey = replyKey;
		if (finished && modelId) load(modelId);
	}

	onMount(() => {
		const timer = setInterval(() => {
			if (modelId && document.visibilityState === 'visible') load(modelId);
		}, REFRESH_MS);
		return () => clearInterval(timer);
	});

	$: limits = $modelLimits.modelId === modelId ? $modelLimits.limits : null;
	$: outlook = limits?.outlook ?? [];
	// Outer ring: the 5-hour window, or a key's credit limit; inner ring: the weekly window.
	$: rings = [
		outlook.find((item) => item.id === 'primary') ?? outlook.find((item) => item.id === 'credits'),
		outlook.find((item) => item.id === 'secondary')
	].filter((item): item is Outlook => !!item);
	$: warning = lowWindows(outlook)[0] ?? null;

	const ringClass = (item: Outlook) =>
		item.level === 'ok' ? 'ring-normal' : item.level === 'low' ? 'ring-warning' : 'ring-critical';

	$: summary = warning
		? warningText(warning, t)
		: rings.length
			? rings
					.map((item) =>
						t('{{WINDOW}}: {{PERCENT}} used', {
							WINDOW: outlookName(item, t),
							PERCENT: formatPercent(item.used_percent)
						})
					)
					.join(' · ')
			: '';

	const portal = (node: HTMLElement) => {
		document.body.appendChild(node);
		const panel = node.firstElementChild as HTMLElement;
		const cleanup = autoUpdate(triggerElement!, node, () => {
			panel.style.maxHeight = '';
			computePosition(triggerElement!, node, {
				strategy: 'fixed',
				placement: 'top-end',
				middleware: [
					offset(4),
					flip({ padding: 8, crossAxis: false }),
					shift({ padding: 8 }),
					size({
						padding: 8,
						apply({ availableHeight, availableWidth }) {
							panel.style.maxHeight = `${Math.max(0, availableHeight)}px`;
							panel.style.maxWidth = `${Math.max(0, availableWidth)}px`;
						}
					})
				]
			}).then(({ x, y }) => {
				node.style.left = `${x}px`;
				node.style.top = `${y}px`;
			});
		});

		return {
			destroy() {
				cleanup();
				node.remove();
			}
		};
	};

	const close = (refocus = false) => {
		show = false;
		if (refocus) triggerElement?.focus();
	};

	const toggle = async () => {
		show = !show;
		if (show) {
			load(modelId);
			await tick();
			contentElement?.focus();
		}
	};

	const handlePointerDown = (e: PointerEvent) => {
		if (!show) return;
		const target = e.target as Node;
		if (triggerElement?.contains(target) || contentElement?.contains(target)) return;
		close();
	};

	const handleKeydown = (e: KeyboardEvent) => {
		if (show && e.key === 'Escape') {
			e.preventDefault();
			e.stopPropagation();
			close(true);
		}
	};

	$: highContrast = $settings?.highContrastMode ?? false;
	// Outer ring radius 8, inner 4.5, on a 20-unit box.
	const RADII = [8, 4.5];
</script>

<svelte:window on:pointerdown={handlePointerDown} on:keydown={handleKeydown} />

{#if modelId && rings.length}
	<div class="relative flex shrink-0 items-center">
		<Tooltip content={show ? '' : summary} placement="top">
			<button
				bind:this={triggerElement}
				id="limits-indicator-button"
				type="button"
				class="limits-viz flex h-9 min-w-9 items-center justify-center gap-1.5 rounded-[9px] px-2 transition-colors duration-100 hover:bg-gray-100 dark:hover:bg-gray-800/60 {highContrast
					? 'focus-ring'
					: 'outline-hidden focus-visible:ring-2 focus-visible:ring-gray-900 dark:focus-visible:ring-accent'}"
				aria-label={$i18n.t('Usage limits: {{SUMMARY}}', { SUMMARY: summary })}
				aria-haspopup="dialog"
				aria-expanded={show}
				on:click={toggle}
			>
				<svg viewBox="0 0 20 20" class="size-[18px] -rotate-90" aria-hidden="true">
					{#each rings as item, index (item.id)}
						{@const radius = RADII[index]}
						{@const circumference = 2 * Math.PI * radius}
						<circle cx="10" cy="10" r={radius} fill="none" stroke-width="2.25" class="ring-track" />
						{#if item.used_percent > 0}
							<circle
								cx="10"
								cy="10"
								r={radius}
								fill="none"
								stroke-width="2.25"
								stroke-linecap="round"
								class="ring-value {ringClass(item)}"
								stroke-dasharray={`${(Math.max(item.used_percent, 4) / 100) * circumference} ${circumference}`}
							/>
						{/if}
					{/each}
				</svg>
				{#if warning}
					<span
						class="hidden whitespace-nowrap font-mono text-[11px] sm:inline {warning.level === 'low'
							? 'text-warning'
							: 'text-critical'}">{shortWarning(warning, t)}</span
					>
				{/if}
			</button>
		</Tooltip>

		{#if show}
			<div use:portal style="position: fixed; z-index: 9999; top: 0; left: 0; width: max-content;">
				<div
					bind:this={contentElement}
					class="limits-viz flex w-[20rem] max-w-[calc(100vw-1rem)] flex-col gap-3 overflow-y-auto rounded-xl border border-gray-200 bg-white p-3 text-gray-700 shadow-lg outline-hidden dark:border-gray-800 dark:bg-gray-900 dark:text-gray-100 dark:shadow-[0_12px_32px_-12px_rgba(0,0,0,0.6)]"
					role="dialog"
					tabindex="-1"
					aria-label={$i18n.t('Usage limits')}
					transition:flyAndScale
				>
					<div class="flex flex-col gap-0.5">
						<div class="flex items-baseline justify-between gap-3">
							<div class="text-[13px] font-medium text-gray-900 dark:text-gray-100">
								{$i18n.t('Usage limits')}
							</div>
							{#if limits?.account}
								<div class="truncate font-mono text-[11.5px] text-gray-500 dark:text-gray-400">
									{limits.account.label}
								</div>
							{/if}
						</div>
						{#if limits?.account}
							<div class="text-[11.5px] text-gray-500 dark:text-gray-400">
								{providers[limits.account.provider] ?? limits.account.provider}
								{#if (limits.accounts ?? 1) > 1}
									· {$i18n.t('next of {{COUNT}} accounts for this model', {
										COUNT: limits.accounts
									})}
								{/if}
							</div>
						{/if}
					</div>

					{#each outlook.filter((item) => item.level !== 'ok') as item (item.id)}
						<div
							role="status"
							class="rounded-lg px-2.5 py-2 text-[12px] leading-4 {item.level === 'low'
								? 'bg-amber-50 text-amber-800 dark:bg-amber-400/10 dark:text-amber-200'
								: 'bg-red-50 text-red-800 dark:bg-red-500/10 dark:text-red-200'}"
						>
							{warningText(item, t)}
						</div>
					{/each}

					{#if (limits?.accounts ?? 1) > 1}
						<div class="flex flex-col gap-0.5 text-[12px]">
							<div class="text-gray-500 dark:text-gray-400">
								{$i18n.t('All {{COUNT}} accounts together', { COUNT: limits?.accounts ?? 0 })}
							</div>
							{#each rings as item (item.id)}
								<div class="flex items-baseline justify-between gap-3">
									<span>{outlookName(item, t)}</span>
									<span class="font-mono">{formatPercent(item.used_percent)}</span>
								</div>
							{/each}
						</div>
						<div class="h-px bg-gray-100 dark:bg-gray-800"></div>
						<div class="text-[12px] text-gray-500 dark:text-gray-400">
							{$i18n.t('Next request')}
						</div>
					{/if}

					{#if limits?.windows?.length}
						<ProviderLimitsList windows={limits.windows} />
					{/if}

					{#if limits?.account}
						<button
							type="button"
							class="self-start text-[12px] text-gray-500 underline-offset-2 hover:text-gray-900 hover:underline dark:text-gray-400 dark:hover:text-gray-100"
							on:click={() => {
								close();
								showSettings.set('provider_accounts');
							}}>{$i18n.t('Provider accounts')}</button
						>
					{/if}
				</div>
			</div>
		{/if}
	</div>
{/if}

<style>
	/* The context indicator's colors: neutral while there is room, then warning and critical. */
	.limits-viz {
		--track: #e8e8ea;
		--value: #3f3f46;
		--warning: #b47d00;
		--warning-ring: #fab219;
		--critical: #d03b3b;
	}
	:global(.dark) .limits-viz {
		--track: #2a2b36;
		--value: #d4d4d8;
		--warning: #fab219;
	}

	.ring-track {
		stroke: var(--track);
	}
	.ring-value {
		transition: stroke-dasharray 200ms ease;
	}
	.ring-normal {
		stroke: var(--value);
	}
	.ring-warning {
		stroke: var(--warning-ring);
	}
	.ring-critical {
		stroke: var(--critical);
	}
	.text-warning {
		color: var(--warning);
	}
	.text-critical {
		color: var(--critical);
	}
</style>
