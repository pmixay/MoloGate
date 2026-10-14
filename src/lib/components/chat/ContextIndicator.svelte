<script lang="ts">
	// Mologate: context indicator, the ring to the right of the effort selector. The ring shows
	// how full the selected model's context window is; opening it shows what fills the window
	// (see utils/contextBreakdown.ts) and the usage limits the model's provider account reported
	// (utils/provider_limits.py on the server). The limits go to a store, so the low-limit
	// warning above the message box (LimitsWarning) reads them from here too. Mologate, phase 4:
	// the panel also shows what the member has left of the model (their own account plus their share
	// of the team pool), their tier and credits.
	import { getContext, onMount, tick } from 'svelte';
	import { autoUpdate, computePosition, flip, offset, shift, size } from '@floating-ui/dom';

	import { modelLimits, models, settings } from '$lib/stores';
	import { getModelLimits } from '$lib/apis/models';
	import { createMessagesList } from '$lib/utils';
	import {
		buildContextBreakdown,
		CONTEXT_CATEGORIES,
		type ContextCategory
	} from '$lib/utils/contextBreakdown';
	import { flyAndScale } from '$lib/utils/transitions';

	import {
		formatDuration,
		formatPercent,
		observing,
		roomFormula,
		shareWindowName,
		tierLabel
	} from '$lib/utils/providerLimits';

	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import ProviderLimitsList from './ProviderLimitsList.svelte';

	const i18n: any = getContext('i18n');

	export let selectedModels: string[] = [];
	export let history: { messages: Record<string, any>; currentId: string | null } = {
		messages: {},
		currentId: null
	};
	// Chat.svelte's usage estimate: the fallback total for older chats and the compaction threshold.
	export let contextUsage: { tokens?: number; threshold?: number | null } | null = null;

	const RING_RADIUS = 7;
	const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS;
	const REFRESH_MS = 60_000;

	let show = false;
	let triggerElement: HTMLElement | null = null;
	let contentElement: HTMLElement | null = null;

	let limitsLoadedFor = '';
	let limitsRequestId = 0;

	$: modelId = selectedModels?.[0] ?? '';
	$: model = ($models ?? []).find((m: any) => m.id === modelId) as any;
	$: compactThreshold =
		Number(contextUsage?.threshold) > 0 ? Number(contextUsage?.threshold) : null;
	$: contextWindow = Number(model?.context_window) > 0 ? Number(model.context_window) : null;
	// Without a known window, measure against the point where the chat compacts.
	$: capacity = contextWindow ?? compactThreshold;

	$: messages = history?.currentId ? createMessagesList(history, history.currentId) : [];
	$: breakdown = buildContextBreakdown(messages, Number(contextUsage?.tokens) || 0);
	$: used = breakdown?.total ?? 0;
	$: percent = capacity ? Math.min(100, (used / capacity) * 100) : null;
	$: level =
		percent === null ? 'normal' : percent >= 90 ? 'critical' : percent >= 75 ? 'warning' : 'normal';

	const categoryLabels: Record<ContextCategory, string> = {
		system: 'System',
		tools: 'Tools',
		summary: 'Summary',
		messages: 'Messages',
		tool_activity: 'Tool calls',
		images: 'Images'
	};

	$: rows = breakdown
		? CONTEXT_CATEGORIES.filter((category) => breakdown.categories[category] > 0).map(
				(category) => ({
					category,
					label: categoryLabels[category],
					tokens: breakdown.categories[category],
					share: (breakdown.categories[category] / (capacity ?? (used || 1))) * 100
				})
			)
		: [];
	$: free = capacity ? Math.max(0, capacity - used) : null;

	const trimNumber = (value: number) =>
		value >= 10 ? String(Math.round(value)) : value.toFixed(1).replace(/\.0$/, '');

	const formatTokens = (value: number) => {
		if (value >= 1_000_000) return `${trimNumber(value / 1_000_000)}m`;
		if (value >= 1_000) return `${trimNumber(value / 1_000)}k`;
		return String(Math.round(value ?? 0));
	};

	const loadLimits = async (id: string) => {
		const current = ++limitsRequestId;
		const result = id ? await getModelLimits(localStorage.token, id).catch(() => null) : null;
		// A newer request (another model, a later reply) wins.
		if (current === limitsRequestId) {
			modelLimits.set({ modelId: id, limits: result });
		}
	};

	$: if (modelId !== limitsLoadedFor) {
		modelLimits.set({ modelId, limits: null });
		limitsLoadedFor = modelId;
		loadLimits(modelId);
	}

	// Each reply moves the limits: read them again once the latest message is finished.
	$: replyKey = history?.currentId
		? `${history.currentId}:${history.messages?.[history.currentId]?.done ?? true}`
		: '';
	let lastReplyKey = '';
	$: if (replyKey !== lastReplyKey) {
		const finished = replyKey.endsWith(':true') && lastReplyKey !== '';
		lastReplyKey = replyKey;
		if (finished && modelId) loadLimits(modelId);
	}

	onMount(() => {
		const timer = setInterval(() => {
			if (modelId && document.visibilityState === 'visible') loadLimits(modelId);
		}, REFRESH_MS);
		return () => clearInterval(timer);
	});

	$: limits = $modelLimits.modelId === modelId ? $modelLimits.limits : null;
	$: allowance = limits?.allowance && limits.allowance.mode !== 'off' ? limits.allowance : null;
	$: t = (key: string, values?: Record<string, string | number>) => $i18n.t(key, values);
	const shareLevel = (level: string) =>
		level === 'empty' ? 'critical' : level === 'low' ? 'warning' : 'normal';

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
			loadLimits(modelId);
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
	$: ringLabel = percent !== null ? formatPercent(percent) : used > 0 ? formatTokens(used) : '';
	$: summary =
		percent !== null
			? $i18n.t('Context: {{PERCENT}} of {{WINDOW}}', {
					PERCENT: formatPercent(percent),
					WINDOW: formatTokens(capacity ?? 0)
				})
			: $i18n.t('Context: {{TOKENS}} tokens', { TOKENS: formatTokens(used) });
</script>

<svelte:window on:pointerdown={handlePointerDown} on:keydown={handleKeydown} />

{#if modelId}
	<div class="relative flex shrink-0 items-center">
		<Tooltip content={show ? '' : summary} placement="top">
			<button
				bind:this={triggerElement}
				id="context-indicator-button"
				type="button"
				class="context-viz flex h-9 min-w-9 items-center justify-center gap-1.5 rounded-[9px] px-2 transition-colors duration-100 hover:bg-gray-100 dark:hover:bg-gray-800/60 {highContrast
					? 'focus-ring'
					: 'outline-hidden focus-visible:ring-2 focus-visible:ring-gray-900 dark:focus-visible:ring-accent'}"
				aria-label={summary}
				aria-haspopup="dialog"
				aria-expanded={show}
				on:click={toggle}
			>
				<!-- Mologate: lifted 1px so the ring lines up with the model name's capitals, not its box. -->
				<svg viewBox="0 0 20 20" class="size-[18px] -translate-y-px -rotate-90" aria-hidden="true">
					<circle
						cx="10"
						cy="10"
						r={RING_RADIUS}
						fill="none"
						stroke-width="2.5"
						class="ring-track"
					/>
					{#if percent !== null && percent > 0}
						<circle
							cx="10"
							cy="10"
							r={RING_RADIUS}
							fill="none"
							stroke-width="2.5"
							stroke-linecap="round"
							class="ring-value ring-{level}"
							stroke-dasharray={`${(Math.max(percent, 4) / 100) * RING_CIRCUMFERENCE} ${RING_CIRCUMFERENCE}`}
						/>
					{/if}
				</svg>
				<!-- The number keeps a nearly empty ring from reading as a loading spinner. -->
				{#if ringLabel}
					<span class="hidden font-mono text-[11px] text-gray-500 sm:inline dark:text-gray-400"
						>{ringLabel}</span
					>
				{/if}
			</button>
		</Tooltip>

		{#if show}
			<div use:portal style="position: fixed; z-index: 9999; top: 0; left: 0; width: max-content;">
				<div
					bind:this={contentElement}
					class="context-viz flex w-[20rem] max-w-[calc(100vw-1rem)] flex-col gap-3 overflow-y-auto rounded-xl border border-gray-200 bg-white p-3 text-gray-700 shadow-lg outline-hidden dark:border-gray-800 dark:bg-gray-900 dark:text-gray-100 dark:shadow-[0_12px_32px_-12px_rgba(0,0,0,0.6)]"
					role="dialog"
					tabindex="-1"
					aria-label={$i18n.t('Context')}
					transition:flyAndScale
				>
					<div class="flex flex-col gap-1.5">
						<div class="flex items-baseline justify-between gap-3">
							<div class="text-[13px] font-medium text-gray-900 dark:text-gray-100">
								{$i18n.t('Context')}
							</div>
							<div class="font-mono text-[13px] text-gray-900 dark:text-gray-100">
								{percent !== null ? formatPercent(percent) : formatTokens(used)}
							</div>
						</div>
						{#if capacity}
							<div class="font-mono text-[11.5px] text-gray-500 dark:text-gray-400">
								{formatTokens(used)} / {formatTokens(capacity)}
							</div>
						{/if}

						{#if rows.length > 0}
							<!-- Stacked bar: one segment per category, then free space. -->
							<div
								class="mt-1 flex h-2 w-full gap-[2px] overflow-hidden rounded-full"
								aria-hidden="true"
							>
								{#each rows as row (row.category)}
									<div
										class="h-full min-w-[3px] series-{row.category}"
										style="flex: {Math.max(row.share, 0.5)} 1 0%"
									></div>
								{/each}
								{#if free !== null && capacity}
									<div
										class="ring-track-fill h-full"
										style="flex: {(free / capacity) * 100} 1 0%"
									></div>
								{/if}
							</div>

							<table class="mt-1 w-full text-[12px]">
								<tbody>
									{#each rows as row (row.category)}
										<tr>
											<td class="py-0.5 pr-2">
												<span class="flex items-center gap-2">
													<span class="size-2 shrink-0 rounded-[2px] series-{row.category}"></span>
													<span class="truncate">{$i18n.t(row.label)}</span>
												</span>
											</td>
											<td class="py-0.5 pr-2 text-right font-mono text-gray-500 dark:text-gray-400"
												>{formatTokens(row.tokens)}</td
											>
											<td class="w-10 py-0.5 text-right font-mono">{formatPercent(row.share)}</td>
										</tr>
									{/each}
									{#if free !== null && capacity}
										<tr class="text-gray-500 dark:text-gray-400">
											<td class="py-0.5 pr-2">
												<span class="flex items-center gap-2">
													<span class="ring-track-fill size-2 shrink-0 rounded-[2px]"></span>
													<span>{$i18n.t('Free')}</span>
												</span>
											</td>
											<td class="py-0.5 pr-2 text-right font-mono">{formatTokens(free)}</td>
											<td class="w-10 py-0.5 text-right font-mono"
												>{formatPercent((free / capacity) * 100)}</td
											>
										</tr>
									{/if}
								</tbody>
							</table>
						{:else}
							<div class="text-[12px] text-gray-500 dark:text-gray-400">
								{$i18n.t('No usage yet')}
							</div>
						{/if}
					</div>

					{#if allowance}
						<!-- Mologate: the member's share of the team pool (docs/limits-and-accounts.md §7). -->
						<div class="h-px bg-gray-100 dark:bg-gray-800"></div>

						<div class="flex flex-col gap-2">
							<div class="flex items-baseline justify-between gap-3">
								<div class="text-[13px] font-medium text-gray-900 dark:text-gray-100">
									{$i18n.t('Fair use')}
								</div>
								<div class="truncate text-[11.5px] text-gray-500 dark:text-gray-400">
									{tierLabel(allowance, t)}
								</div>
							</div>
							{#if observing(allowance)}
								<div class="text-[11.5px] text-gray-500 dark:text-gray-400">
									{$i18n.t(
										'Not enforced yet: this is what the rules would do. Nothing is limited for now.'
									)}
								</div>
							{/if}
							<div class="text-[11.5px] text-gray-500 dark:text-gray-400">
								{Object.values(allowance.windows).some((window) => window.own.allowed > 0)
									? $i18n.t(
											'What you can still use of {{MODEL}}: your own account plus your share of the team pool.',
											{ MODEL: allowance.asked_name || allowance.model_name }
										)
									: $i18n.t('What you can still use of {{MODEL}}: your share of the team pool.', {
											MODEL: allowance.asked_name || allowance.model_name
										})}
							</div>
							{#each Object.entries(allowance.windows) as [name, window] (name)}
								<div class="flex flex-col gap-1">
									<div class="flex items-baseline justify-between gap-3 text-[12px]">
										<span
											>{shareWindowName(
												name,
												t
											)}{#if allowance.limit === name && window.level !== 'ok'}
												<span class="ml-1 text-gray-500 dark:text-gray-400"
													>· {$i18n.t('runs out first')}</span
												>{/if}</span
										>
										<span class="font-mono"
											>{$i18n.t('{{PERCENT}} used', {
												PERCENT: formatPercent(window.percent)
											})}</span
										>
									</div>
									<div class="ring-track-fill h-1.5 w-full overflow-hidden rounded-full">
										<div
											class="h-full rounded-full bar-{shareLevel(window.level)}"
											style="width: {Math.min(100, window.percent)}%"
										></div>
									</div>
									<div class="text-[11px] text-gray-500 dark:text-gray-400">
										{roomFormula(window, t)}.
										{#if window.minutes_left !== null && window.level !== 'empty'}
											{$i18n.t('About {{DURATION}} of work left at the recent pace.', {
												DURATION: formatDuration(window.minutes_left * 60)
											})}
										{/if}
										{#if window.resets_at}
											{$i18n.t('Frees up in {{DURATION}}.', {
												DURATION: formatDuration(window.resets_at - Date.now() / 1000)
											})}
										{/if}
									</div>
								</div>
							{/each}
							<div class="flex items-baseline justify-between gap-3 text-[12px]">
								<span>{$i18n.t('Credits')}</span>
								<span class="font-mono">{allowance.credits}</span>
							</div>
							{#if allowance.swapped}
								<div class="text-[11.5px] text-gray-500 dark:text-gray-400">
									{observing(allowance)
										? $i18n.t('Once enforced, replies would use {{MODEL}}.', {
												MODEL: allowance.model_name
											})
										: $i18n.t('Replies use {{MODEL}}.', { MODEL: allowance.model_name })}
								</div>
							{/if}
						</div>
					{/if}

					{#if limits?.windows?.length}
						<div class="h-px bg-gray-100 dark:bg-gray-800"></div>

						<div class="flex flex-col gap-2">
							<div class="text-[13px] font-medium text-gray-900 dark:text-gray-100">
								{$i18n.t('Limits')}
							</div>
							<ProviderLimitsList windows={limits.windows} />
						</div>
					{/if}
				</div>
			</div>
		{/if}
	</div>
{/if}

<style>
	/* Categorical slots 1-6 in fixed order (dataviz reference palette, validated for adjacent
	   pairs on this app's white and gray-900 surfaces); the labeled table is the relief for the
	   light slots under 3:1. Status colors mark a full window or a limit running low. */
	.context-viz {
		--series-system: #2a78d6;
		--series-tools: #eb6834;
		--series-summary: #1baf7a;
		--series-messages: #eda100;
		--series-tool_activity: #e87ba4;
		--series-images: #008300;
		--track: #e8e8ea;
		--value: #3f3f46;
		--warning: #fab219;
		--critical: #d03b3b;
	}
	:global(.dark) .context-viz {
		--series-system: #3987e5;
		--series-tools: #d95926;
		--series-summary: #199e70;
		--series-messages: #c98500;
		--series-tool_activity: #d55181;
		--series-images: #008300;
		--track: #2a2b36;
		--value: #d4d4d8;
	}

	.series-system {
		background: var(--series-system);
	}
	.series-tools {
		background: var(--series-tools);
	}
	.series-summary {
		background: var(--series-summary);
	}
	.series-messages {
		background: var(--series-messages);
	}
	.series-tool_activity {
		background: var(--series-tool_activity);
	}
	.series-images {
		background: var(--series-images);
	}

	.ring-track {
		stroke: var(--track);
	}
	.ring-track-fill {
		background: var(--track);
	}
	.ring-value {
		transition: stroke-dasharray 200ms ease;
	}
	.ring-normal {
		stroke: var(--value);
	}
	.ring-warning {
		stroke: var(--warning);
	}
	.ring-critical {
		stroke: var(--critical);
	}
	.bar-normal {
		background: var(--value);
	}
	.bar-warning {
		background: var(--warning);
	}
	.bar-critical {
		background: var(--critical);
	}
</style>
