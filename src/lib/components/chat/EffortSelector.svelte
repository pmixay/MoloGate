<script lang="ts">
	// Reasoning effort for the current chat, shown next to the composer's model selector.
	// Levels come from the model listing (`reasoning_effort_options`: admin override first,
	// then provider metadata); the choice lives in the chat's params as `reasoning_effort`,
	// which Chat.svelte sends with every request and saves with the chat.
	import { getContext, tick } from 'svelte';
	import { autoUpdate, computePosition, flip, offset, shift, size } from '@floating-ui/dom';

	import { models, settings } from '$lib/stores';
	import { flyAndScale } from '$lib/utils/transitions';

	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import ChevronDown from '$lib/components/icons/ChevronDown.svelte';
	import Check from '$lib/components/icons/Check.svelte';

	const i18n: any = getContext('i18n');

	// With several models side by side, the first one decides which levels are offered.
	export let selectedModels: string[] = [];
	export let params: Record<string, any> = {};
	export let disabled = false;

	type Level = { value: string; description?: string | null };
	type Option = { value: string | null; label: string; description?: string | null };

	let show = false;
	let triggerElement: HTMLElement | null = null;
	let contentElement: HTMLElement | null = null;

	$: model = ($models ?? []).find((m: any) => m.id === (selectedModels?.[0] ?? ''));
	$: levels = (model?.reasoning_effort_options?.levels ?? []) as Level[];
	$: defaultLevel = (model?.reasoning_effort_options?.default ?? null) as string | null;
	$: chosen = (params?.reasoning_effort ?? null) as string | null;
	$: active = chosen ?? defaultLevel;

	// Without a known default the provider decides; offer that as its own entry.
	$: options = [
		...(defaultLevel
			? []
			: [
					{
						value: null,
						label: $i18n.t('Default'),
						description: $i18n.t('Let the provider choose')
					}
				]),
		...levels.map((level) => ({
			value: level.value,
			label: level.value,
			description: level.description
		}))
	] as Option[];

	const setEffort = (value: string | null) => {
		const { reasoning_effort, ...rest } = params ?? {};
		params = value ? { ...rest, reasoning_effort: value } : rest;
	};

	// Never keep (and send) a level the selected model doesn't offer, e.g. after switching models.
	$: if (model && chosen && !levels.some((level) => level.value === chosen)) {
		setEffort(null);
	}

	const portal = (node: HTMLElement) => {
		document.body.appendChild(node);
		const panel = node.firstElementChild as HTMLElement;
		const cleanup = autoUpdate(triggerElement!, node, () => {
			panel.style.maxHeight = '';
			computePosition(triggerElement!, node, {
				strategy: 'fixed',
				placement: 'bottom-end',
				middleware: [
					offset(2),
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

	const optionButtons = () =>
		Array.from(contentElement?.querySelectorAll<HTMLElement>('[role="option"]') ?? []);

	const close = (refocus = false) => {
		show = false;
		if (refocus) triggerElement?.focus();
	};

	const toggle = async () => {
		show = !show;
		if (show) {
			await tick();
			const buttons = optionButtons();
			(buttons.find((b) => b.getAttribute('aria-selected') === 'true') ?? buttons[0])?.focus();
		}
	};

	const select = (value: string | null) => {
		setEffort(value);
		close(true);
	};

	const handlePointerDown = (e: PointerEvent) => {
		if (!show) return;
		const target = e.target as Node;
		if (triggerElement?.contains(target) || contentElement?.contains(target)) return;
		close();
	};

	const handleKeydown = (e: KeyboardEvent) => {
		if (!show) return;
		if (e.key === 'Escape') {
			e.preventDefault();
			e.stopPropagation();
			close(true);
		} else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
			const buttons = optionButtons();
			if (buttons.length === 0) return;
			e.preventDefault();
			const idx = buttons.indexOf(document.activeElement as HTMLElement);
			const next =
				e.key === 'ArrowDown'
					? (idx + 1) % buttons.length
					: (idx - 1 + buttons.length) % buttons.length;
			buttons[next]?.focus();
		} else if (e.key === 'Tab') {
			close();
		}
	};

	$: highContrast = $settings?.highContrastMode ?? false;
</script>

<svelte:window on:pointerdown={handlePointerDown} on:keydown={handleKeydown} />

{#if levels.length > 0}
	<div class="relative flex shrink-0 items-center">
		<Tooltip
			content={show ? '' : $i18n.t('settings.personal.general.parameters.reasoningEffort.label')}
			placement="top"
		>
			<button
				bind:this={triggerElement}
				id="effort-selector-button"
				type="button"
				class="relative rounded-[9px] {highContrast
					? 'focus-ring'
					: 'outline-hidden focus-visible:ring-2 focus-visible:ring-gray-900 dark:focus-visible:ring-accent'}"
				aria-label={$i18n.t('Reasoning effort: {{level}}', {
					level: active ?? $i18n.t('Default')
				})}
				aria-haspopup="listbox"
				aria-expanded={show}
				{disabled}
				on:click={toggle}
			>
				<div
					class="flex h-9 items-center gap-2 rounded-[9px] pl-2.5 pr-2 font-mono text-[13px] font-normal text-gray-700 transition-colors duration-100 hover:bg-gray-100 hover:text-gray-900 dark:text-gray-100 dark:hover:bg-gray-800/60 dark:hover:text-gray-100"
				>
					<span class="whitespace-nowrap">{active ?? $i18n.t('Default')}</span>
					<ChevronDown
						className="ml-0.5 size-3 shrink-0 self-center text-gray-500"
						strokeWidth="2.25"
					/>
				</div>
			</button>
		</Tooltip>

		{#if show}
			<div
				use:portal
				bind:this={contentElement}
				style="position: fixed; z-index: 9999; top: 0; left: 0; width: max-content;"
			>
				<div
					class="flex w-[17rem] max-w-[calc(100vw-1rem)] flex-col overflow-y-auto rounded-xl border border-gray-200 bg-white p-1 shadow-lg outline-hidden dark:border-gray-800 dark:bg-gray-900 dark:text-gray-100 dark:shadow-[0_12px_32px_-12px_rgba(0,0,0,0.6)]"
					role="listbox"
					aria-label={$i18n.t('settings.personal.general.parameters.reasoningEffort.label')}
					transition:flyAndScale
				>
					<div class="px-2 pb-1 pt-1.5 text-[11px] text-gray-500">
						{$i18n.t('settings.personal.general.parameters.reasoningEffort.label')}
					</div>

					{#each options as option (option.value ?? '')}
						{@const selected = option.value === active}
						<button
							type="button"
							role="option"
							aria-selected={selected}
							class="focus-ring flex min-h-8 w-full cursor-pointer select-none items-center gap-2 rounded-lg px-2 py-1.5 text-left outline-hidden transition-colors duration-75 {highContrast
								? 'hover:bg-gray-200 focus-visible:bg-gray-200 dark:hover:bg-gray-800 dark:focus-visible:bg-gray-800'
								: 'hover:bg-gray-100 focus-visible:bg-gray-100 dark:hover:bg-gray-850 dark:focus-visible:bg-gray-850'} {selected
								? highContrast
									? 'bg-gray-200 dark:bg-gray-800'
									: 'bg-gray-100 dark:bg-[#1a1b25]'
								: ''}"
							on:click={() => select(option.value)}
						>
							<div class="flex min-w-0 flex-1 flex-col gap-0.5">
								<div
									class="flex items-center gap-2 font-mono text-[13px] font-normal text-gray-700 dark:text-gray-100"
								>
									<span class="truncate">{option.label}</span>
									{#if option.value && option.value === defaultLevel}
										<span class="shrink-0 font-sans text-[11px] text-gray-500"
											>{$i18n.t('Default')}</span
										>
									{/if}
								</div>
								{#if option.description}
									<div class="text-[11.5px] leading-4 text-gray-500 dark:text-gray-400">
										{option.description}
									</div>
								{/if}
							</div>

							{#if selected}
								<div class="shrink-0 text-gray-900 dark:text-accent">
									<Check className="size-3.5" strokeWidth="2.5" />
								</div>
							{/if}
						</button>
					{/each}
				</div>
			</div>
		{/if}
	</div>
{/if}
