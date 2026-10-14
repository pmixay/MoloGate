<script lang="ts">
	import { createEventDispatcher, getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Check from '$lib/components/icons/Check.svelte';
	import ChevronLeft from '$lib/components/icons/ChevronLeft.svelte';
	import ChevronRight from '$lib/components/icons/ChevronRight.svelte';

	const i18n: Writable<i18nType> = getContext('i18n');
	const dispatch = createEventDispatcher();

	type AskUserOption = {
		label: string;
		description: string;
	};

	type AskUserQuestion = {
		id: string;
		header: string;
		question: string;
		options: AskUserOption[];
		allow_other?: boolean;
	};

	type DraftAnswer =
		| {
				type: 'option';
				option_index: number;
				label: string;
				description: string;
		  }
		| {
				type: 'other';
				text: string;
		  };

	// Mologate: up to five questions with up to five options each. There is no timer: the card
	// stays until the user answers or cancels, and the agent waits for it.
	export let show = false;
	export let questions: AskUserQuestion[] = [];
	export let allowOther = true;

	let answers: Record<string, DraftAnswer> = {};
	let questionIndex = 0;
	let wasOpen = false;

	const cancel = () => {
		show = false;
		dispatch('cancel');
	};

	$: if (show && !wasOpen) {
		answers = {};
		questionIndex = 0;
		wasOpen = true;
	}

	$: if (!show && wasOpen) {
		wasOpen = false;
	}

	const questionAllowsOther = (question: AskUserQuestion) => question.allow_other ?? allowOther;

	$: question = questions[questionIndex];

	const isAnswered = (question: AskUserQuestion, selected = answers) => {
		const answer = selected[question.id];
		return answer?.type === 'option' || (answer?.type === 'other' && answer.text.trim() !== '');
	};

	const hasAnswers = (selected = answers) =>
		questions.length > 0 && questions.every((question) => isAnswered(question, selected));

	$: complete = hasAnswers(answers);

	const submit = (selected = answers) => {
		if (!hasAnswers(selected)) {
			return;
		}

		const normalized: Record<string, DraftAnswer> = {};
		for (const question of questions) {
			const answer = selected[question.id];
			if (answer?.type === 'option') {
				normalized[question.id] = answer;
			} else if (answer?.type === 'other') {
				normalized[question.id] = {
					type: 'other',
					text: answer.text.trim()
				};
			}
		}

		show = false;
		dispatch('confirm', {
			status: 'answered',
			answers: normalized
		});
	};

	const advance = (selected = answers) => {
		if (questionIndex < questions.length - 1) {
			questionIndex += 1;
		} else if (hasAnswers(selected)) {
			submit(selected);
		}
	};

	const selectOption = (question: AskUserQuestion, option: AskUserOption, index: number) => {
		const selected: Record<string, DraftAnswer> = {
			...answers,
			[question.id]: {
				type: 'option',
				option_index: index,
				label: option.label,
				description: option.description
			}
		};
		answers = selected;
		advance(selected);
	};

	const selectOther = (question: AskUserQuestion) => {
		const existing = answers[question.id];
		answers = {
			...answers,
			[question.id]: {
				type: 'other',
				text: existing?.type === 'other' ? existing.text : ''
			}
		};
	};

	const updateOther = (question: AskUserQuestion, text: string) => {
		answers = {
			...answers,
			[question.id]: {
				type: 'other',
				text
			}
		};
	};

	const isSelectedOption = (question: AskUserQuestion, index: number) => {
		const answer = answers[question.id];
		return answer?.type === 'option' && answer.option_index === index;
	};

	const isSelectedOther = (question: AskUserQuestion) => answers[question.id]?.type === 'other';

	const otherText = (question: AskUserQuestion) => {
		const answer = answers[question.id];
		return answer?.type === 'other' ? answer.text : '';
	};
</script>

{#if show}
	<section class="my-1 rounded-2xl bg-gray-50/70 px-3.5 py-3 dark:bg-white/[0.035]">
		<div class="space-y-2">
			{#if question}
				{#key question.id}
					<div class="space-y-2">
						<div>
							<div class="flex items-center justify-between gap-3">
								<div class="text-sm font-medium text-gray-900 dark:text-gray-100">
									{question.header}
								</div>
								{#if questions.length > 1}
									<div class="flex shrink-0 items-center gap-0.5">
										{#each questions as step, stepIndex (step.id)}
											<Tooltip content={step.header} placement="top">
												<button
													type="button"
													class="flex size-5 items-center justify-center rounded-full text-[0.625rem] tabular-nums transition-colors {stepIndex ===
													questionIndex
														? 'bg-gray-900 text-white dark:bg-white dark:text-black'
														: isAnswered(step, answers)
															? 'bg-gray-200/80 text-gray-700 hover:bg-gray-300/70 dark:bg-white/[0.1] dark:text-gray-200 dark:hover:bg-white/[0.16]'
															: 'text-gray-500 hover:bg-gray-200/60 hover:text-gray-800 dark:text-gray-400 dark:hover:bg-white/[0.06] dark:hover:text-gray-100'}"
													aria-label={step.header}
													aria-current={stepIndex === questionIndex ? 'step' : undefined}
													on:click={() => (questionIndex = stepIndex)}
												>
													{#if stepIndex !== questionIndex && isAnswered(step, answers)}
														<Check className="size-3" strokeWidth="2.5" />
													{:else}
														{stepIndex + 1}
													{/if}
												</button>
											</Tooltip>
										{/each}
									</div>
								{/if}
							</div>
							<div class="mt-0.5 text-xs leading-relaxed text-gray-600 dark:text-gray-300">
								{question.question}
							</div>
						</div>

						<div class="space-y-1">
							<div class="space-y-0.5">
								{#each question.options || [] as option, optionIndex}
									<button
										type="button"
										class="group flex w-full min-w-0 items-baseline gap-2 rounded-lg py-1.5 text-left transition-colors {isSelectedOption(
											question,
											optionIndex
										)
											? 'text-gray-950 dark:text-white'
											: 'text-gray-700 hover:text-gray-950 dark:text-gray-300 dark:hover:text-white'}"
										on:click={() => selectOption(question, option, optionIndex)}
									>
										<span class="min-w-0 shrink-0 text-xs">{option.label}</span>
										<Tooltip
											as="span"
											className="min-w-0 flex-1"
											content={option.description}
											placement="top-start"
										>
											<span
												class="block truncate text-xs leading-relaxed text-gray-500 transition-colors group-hover:text-gray-700 dark:text-gray-400 dark:group-hover:text-gray-300"
											>
												{option.description}
											</span>
										</Tooltip>
										{#if optionIndex === 0}
											<span
												class="shrink-0 rounded-full bg-gray-200/70 px-1.5 py-0.5 text-[0.625rem] text-gray-500 dark:bg-white/[0.08] dark:text-gray-400"
											>
												{$i18n.t('Recommended')}
											</span>
										{/if}
									</button>
								{/each}
							</div>

							{#if questionAllowsOther(question)}
								<div class="flex min-w-0 items-baseline gap-2 rounded-lg py-1.5">
									<div
										class="shrink-0 text-xs {isSelectedOther(question)
											? 'text-gray-950 dark:text-white'
											: 'text-gray-700 dark:text-gray-300'}"
									>
										{$i18n.t('Other')}
									</div>
									<input
										class="min-w-0 flex-1 bg-transparent text-xs text-gray-800 outline-hidden placeholder:text-gray-400 dark:text-gray-100 dark:placeholder:text-gray-500"
										placeholder={$i18n.t('Type your answer')}
										value={otherText(question)}
										on:focus={() => selectOther(question)}
										on:input={(event) =>
											updateOther(question, (event.currentTarget as HTMLInputElement).value)}
										on:keydown={(event) => {
											if (event.key === 'Enter') {
												event.preventDefault();
												advance();
											}
										}}
									/>
								</div>
							{/if}
						</div>
					</div>
				{/key}
			{/if}

			<div class="flex items-center justify-between gap-2">
				<button
					type="button"
					class="rounded-full py-1 pr-2.5 text-xs text-gray-500 transition-colors hover:text-gray-800 dark:text-gray-400 dark:hover:text-gray-100"
					on:click={cancel}
				>
					{$i18n.t('Cancel')}
				</button>
				<div class="flex items-center gap-0.5">
					{#if questionIndex === questions.length - 1 || complete}
						<button
							type="button"
							class="mr-1.5 rounded-full bg-gray-900 px-3 py-1 text-xs font-medium text-white transition hover:opacity-90 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40 dark:bg-white dark:text-black"
							disabled={!complete}
							on:click={() => submit()}
						>
							{$i18n.t('Submit answers')}
						</button>
					{/if}
					{#if questions.length > 1}
						<button
							type="button"
							class="flex size-7 items-center justify-center rounded-full text-gray-600 transition-colors hover:bg-gray-200/60 hover:text-gray-900 disabled:pointer-events-none disabled:opacity-30 dark:text-gray-300 dark:hover:bg-white/[0.06] dark:hover:text-white"
							aria-label={$i18n.t('Previous')}
							disabled={questionIndex === 0}
							on:click={() => (questionIndex -= 1)}
						>
							<ChevronLeft className="size-4" strokeWidth="2" />
						</button>
						<button
							type="button"
							class="flex size-7 items-center justify-center rounded-full text-gray-600 transition-colors hover:bg-gray-200/60 hover:text-gray-900 disabled:pointer-events-none disabled:opacity-30 dark:text-gray-300 dark:hover:bg-white/[0.06] dark:hover:text-white"
							aria-label={$i18n.t('Next')}
							disabled={questionIndex === questions.length - 1}
							on:click={() => (questionIndex += 1)}
						>
							<ChevronRight className="size-4" strokeWidth="2" />
						</button>
					{/if}
				</div>
			</div>
		</div>
	</section>
{/if}
