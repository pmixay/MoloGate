<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';

	import {
		getSubagentsConfig,
		getTaskLimitsConfig,
		setSubagentsConfig,
		setTaskLimitsConfig,
		type TaskLimit
	} from '$lib/apis/configs';
	import { models } from '$lib/stores';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import AdminSettingSection from './AdminSettingSection.svelte';

	const i18n: any = getContext('i18n');

	// Mologate: preset roles the main agent picks when it delegates (backend utils/subagent_profiles.py).
	type Profile = { name: string; description: string; prompt: string; model?: string | null };

	let loading = true;
	let saving = false;
	let enabled = true;
	let backgroundEnabled = true;
	let builtinProfiles: Profile[] = [];
	let profiles: Profile[] = [];
	let maxConcurrent = 20;
	let maxAsync = 20;
	let maxIterations = 30;
	let maxOutput = 30000;
	let systemPrompt = '';

	// Mologate: limits for sub-agents, background work, monitors and schedules
	// (utils/task_limits.py). Times are stored in seconds and entered in the unit that reads best;
	// the server keeps every value in its range.
	let taskLimits: TaskLimit[] = [];
	let taskLimitValues: Record<string, number | null> = {};

	const LIMIT_FACTORS: Record<string, number> = {
		auto_background_seconds: 60,
		background_run_seconds: 60,
		monitor_lifetime_seconds: 3600,
		schedule_interval_seconds: 60,
		schedule_ahead_seconds: 86400,
		schedule_run_seconds: 60,
		schedule_lifetime_seconds: 86400
	};
	const limitFactor = (name: string) => LIMIT_FACTORS[name] ?? 1;
	const toDisplay = (name: string, value: number) =>
		Math.round((value / limitFactor(name)) * 1000) / 1000;

	const applyTaskLimits = (limits: TaskLimit[]) => {
		taskLimits = limits;
		taskLimitValues = Object.fromEntries(
			limits.map((limit) => [limit.name, toDisplay(limit.name, limit.value)])
		);
	};

	$: limitViews = {
		auto_background_seconds: {
			label: $i18n.t('Move a sub-agent or monitor to the background after'),
			unit: $i18n.t('minutes'),
			hint: $i18n.t('0 = never')
		},
		subagents_per_chat: {
			label: $i18n.t('Sub-agents at once in a chat'),
			unit: $i18n.t('sub-agents')
		},
		background_tasks_per_user: {
			label: $i18n.t('Background tasks per member'),
			unit: $i18n.t('tasks')
		},
		background_run_seconds: {
			label: $i18n.t('Longest background run'),
			unit: $i18n.t('minutes')
		},
		monitors_per_user: { label: $i18n.t('Active monitors per member'), unit: $i18n.t('monitors') },
		monitor_interval_seconds: {
			label: $i18n.t('Time between checks'),
			unit: $i18n.t('seconds'),
			hint: $i18n.t('The default, and the shortest an agent can ask for')
		},
		monitor_check_timeout_seconds: {
			label: $i18n.t('Time limit for one check'),
			unit: $i18n.t('seconds')
		},
		monitor_lifetime_seconds: { label: $i18n.t('Longest a monitor waits'), unit: $i18n.t('hours') },
		monitor_max_checks: { label: $i18n.t('Most checks per monitor'), unit: $i18n.t('checks') },
		schedules_per_user: {
			label: $i18n.t('Active schedules per member'),
			unit: $i18n.t('schedules'),
			hint: $i18n.t('Automations count too')
		},
		schedule_interval_seconds: {
			label: $i18n.t('Shortest repeat'),
			unit: $i18n.t('minutes'),
			hint: $i18n.t('For automations too')
		},
		schedule_ahead_seconds: {
			label: $i18n.t('Furthest ahead a task can be scheduled'),
			unit: $i18n.t('days')
		},
		schedule_run_seconds: { label: $i18n.t('Time limit for one run'), unit: $i18n.t('minutes') },
		schedule_lifetime_seconds: {
			label: $i18n.t('Longest a schedule lasts'),
			unit: $i18n.t('days')
		},
		schedule_max_failures: {
			label: $i18n.t('Turn a schedule off after this many failed runs in a row'),
			unit: $i18n.t('runs')
		},
		schedule_low_limit_percent: {
			label: $i18n.t('Hold a run while the model has less than this much of its limit left'),
			unit: '%',
			hint: $i18n.t('0 = off')
		}
	} as Record<string, { label: string; unit: string; hint?: string }>;

	$: limitGroups = [
		{
			title: $i18n.t('Sub-agents and background work'),
			limits: taskLimits.filter(
				(limit) => !limit.name.startsWith('monitor') && !limit.name.startsWith('schedule')
			)
		},
		{
			title: $i18n.t('Monitors'),
			limits: taskLimits.filter((limit) => limit.name.startsWith('monitor'))
		},
		{
			title: $i18n.t('Scheduled tasks'),
			limits: taskLimits.filter((limit) => limit.name.startsWith('schedule'))
		}
	].filter((group) => group.limits.length > 0);

	onMount(async () => {
		getTaskLimitsConfig(localStorage.token)
			.then((res) => applyTaskLimits(Array.isArray(res?.limits) ? res.limits : []))
			.catch(() => {});

		try {
			const config = await getSubagentsConfig(localStorage.token);
			enabled = config?.ENABLE_SUBAGENTS ?? true;
			backgroundEnabled = config?.SUBAGENTS_BACKGROUND_ENABLED ?? true;
			builtinProfiles = config?.SUBAGENTS_BUILTIN_PROFILES ?? [];
			profiles = (config?.SUBAGENTS_PROFILES ?? []).map((profile: Profile) => ({
				...profile,
				model: profile.model ?? ''
			}));
			maxConcurrent = Number(config?.SUBAGENTS_MAX_CONCURRENT) || 20;
			maxAsync = Number(config?.SUBAGENTS_MAX_ASYNC) || 20;
			maxIterations = Number(config?.SUBAGENTS_MAX_ITERATIONS) || 30;
			maxOutput = Number(config?.SUBAGENTS_MAX_OUTPUT) || 30000;
			systemPrompt = config?.SUBAGENTS_SYSTEM_PROMPT ?? '';
		} catch (error) {
			toast.error(`${error}`);
		} finally {
			loading = false;
		}
	});

	const save = async () => {
		saving = true;
		try {
			await setSubagentsConfig(localStorage.token, {
				ENABLE_SUBAGENTS: enabled,
				SUBAGENTS_BACKGROUND_ENABLED: backgroundEnabled,
				SUBAGENTS_MAX_CONCURRENT: maxConcurrent,
				SUBAGENTS_MAX_ASYNC: maxAsync,
				SUBAGENTS_MAX_ITERATIONS: maxIterations,
				SUBAGENTS_MAX_OUTPUT: maxOutput,
				SUBAGENTS_SYSTEM_PROMPT: systemPrompt,
				SUBAGENTS_PROFILES: profiles.map((profile) => ({
					...profile,
					model: profile.model || null
				}))
			});
			if (taskLimits.length > 0) {
				const values = Object.fromEntries(
					taskLimits
						.filter((limit) => Number.isFinite(Number(taskLimitValues[limit.name] ?? NaN)))
						.map((limit) => [
							limit.name,
							Math.round(Number(taskLimitValues[limit.name]) * limitFactor(limit.name))
						])
				);
				const res = await setTaskLimitsConfig(localStorage.token, values);
				if (Array.isArray(res?.limits)) applyTaskLimits(res.limits);
			}
			toast.success($i18n.t('Settings saved successfully!'));
		} catch (error) {
			toast.error(`${(error as any)?.detail ?? error}`);
		} finally {
			saving = false;
		}
	};

	const addProfile = () => {
		profiles = [...profiles, { name: '', description: '', prompt: '', model: '' }];
	};

	const removeProfile = (index: number) => {
		profiles = profiles.filter((_, i) => i !== index);
	};

	const inputClass =
		'h-7 w-full rounded-lg border border-gray-100/50 bg-gray-50/40 px-2 text-xs text-gray-700 outline-hidden transition-colors focus:border-blue-400 dark:border-white/[0.04] dark:bg-white/[0.03] dark:text-gray-300 dark:focus:border-blue-500';
</script>

<form class="flex h-full flex-col justify-between text-sm" on:submit|preventDefault={save}>
	<h2 class="text-sm font-medium text-gray-900 dark:text-white mb-4">
		{$i18n.t('settings.admin.subagents.title')}
	</h2>

	<div class="flex-1 min-h-0 overflow-y-auto scrollbar-hover pr-1.5">
		{#if loading}
			<div class="flex justify-center py-8"><Spinner className="size-6" /></div>
		{:else}
			<div class="flex flex-col gap-2.5">
				<label class="flex cursor-pointer items-center justify-between">
					<span class="text-xs text-gray-600 dark:text-gray-400">
						{$i18n.t('settings.admin.subagents.enableSubAgents.label')}
					</span>
					<Switch bind:state={enabled} />
				</label>
				<p class="-mt-1 text-[0.6875rem] text-gray-400 dark:text-gray-600">
					{$i18n.t(
						'Allow the AI to delegate tasks to sub-agents. Each sub-agent creates a real chat with full tool access. Uses additional LLM calls.'
					)}
				</p>

				{#if enabled}
					<div>
						<label class="text-xs text-gray-600 dark:text-gray-400" for="sa-concurrent">
							{$i18n.t('settings.admin.subagents.maxConcurrent.label')}
						</label>
						<div class="mt-1 flex items-center gap-1.5">
							<input
								id="sa-concurrent"
								type="number"
								bind:value={maxConcurrent}
								min="-1"
								class="h-7 w-16 rounded-lg border border-gray-100/50 bg-gray-50/40 px-2 text-xs text-gray-700 outline-hidden transition-colors focus:border-blue-400 dark:border-white/[0.04] dark:bg-white/[0.03] dark:text-gray-300 dark:focus:border-blue-500"
							/>
							<span class="text-[0.6875rem] text-gray-400 dark:text-gray-600">
								{$i18n.t('simultaneous sub-agents')}
							</span>
						</div>
					</div>

					<div>
						<label class="flex cursor-pointer items-center justify-between">
							<span class="text-xs text-gray-600 dark:text-gray-400">
								{$i18n.t('settings.admin.subagents.enableBackgroundSubAgents.label')}
							</span>
							<Switch bind:state={backgroundEnabled} />
						</label>
						<p class="mt-1 text-[0.6875rem] text-gray-400 dark:text-gray-600">
							{$i18n.t('settings.admin.subagents.enableBackgroundSubAgents.description')}
						</p>
					</div>

					{#if backgroundEnabled}
						<div>
							<label class="text-xs text-gray-600 dark:text-gray-400" for="sa-async">
								{$i18n.t('settings.admin.subagents.maxBackground.label')}
							</label>
							<div class="mt-1 flex items-center gap-1.5">
								<input
									id="sa-async"
									type="number"
									bind:value={maxAsync}
									min="-1"
									class="h-7 w-16 rounded-lg border border-gray-100/50 bg-gray-50/40 px-2 text-xs text-gray-700 outline-hidden transition-colors focus:border-blue-400 dark:border-white/[0.04] dark:bg-white/[0.03] dark:text-gray-300 dark:focus:border-blue-500"
								/>
								<span class="text-[0.6875rem] text-gray-400 dark:text-gray-600">
									{$i18n.t('background sub-agents')}
								</span>
							</div>
						</div>
					{/if}

					<div>
						<label class="text-xs text-gray-600 dark:text-gray-400" for="sa-iterations">
							{$i18n.t('settings.admin.subagents.maxIterations.label')}
						</label>
						<div class="mt-1 flex items-center gap-1.5">
							<input
								id="sa-iterations"
								type="number"
								bind:value={maxIterations}
								min="1"
								max="100"
								class="h-7 w-16 rounded-lg border border-gray-100/50 bg-gray-50/40 px-2 text-xs text-gray-700 outline-hidden transition-colors focus:border-blue-400 dark:border-white/[0.04] dark:bg-white/[0.03] dark:text-gray-300 dark:focus:border-blue-500"
							/>
							<span class="text-[0.6875rem] text-gray-400 dark:text-gray-600">
								{$i18n.t('tool loops per sub-agent')}
							</span>
						</div>
					</div>

					<div>
						<label class="text-xs text-gray-600 dark:text-gray-400" for="sa-output">
							{$i18n.t('settings.admin.subagents.maxOutput.label')}
						</label>
						<div class="mt-1 flex items-center gap-1.5">
							<input
								id="sa-output"
								type="number"
								bind:value={maxOutput}
								min="1000"
								max="100000"
								step="1000"
								class="h-7 w-20 rounded-lg border border-gray-100/50 bg-gray-50/40 px-2 text-xs text-gray-700 outline-hidden transition-colors focus:border-blue-400 dark:border-white/[0.04] dark:bg-white/[0.03] dark:text-gray-300 dark:focus:border-blue-500"
							/>
							<span class="text-[0.6875rem] text-gray-400 dark:text-gray-600">chars</span>
						</div>
					</div>

					<div>
						<label class="text-xs text-gray-600 dark:text-gray-400" for="sa-prompt">
							{$i18n.t('settings.admin.subagents.systemPrompt.label')}
						</label>
						<textarea
							id="sa-prompt"
							bind:value={systemPrompt}
							rows="4"
							placeholder={$i18n.t('You are a sub-agent...')}
							class="mt-1 w-full resize-y rounded-lg border border-gray-100/50 bg-gray-50/40 px-2 py-1.5 font-mono text-xs text-gray-700 outline-hidden transition-colors focus:border-blue-400 dark:border-white/[0.04] dark:bg-white/[0.03] dark:text-gray-300 dark:focus:border-blue-500"
						></textarea>
						<p class="mt-0.5 text-[0.6875rem] text-gray-400 dark:text-gray-600">
							{$i18n.t('settings.admin.subagents.systemPrompt.description')}
						</p>
					</div>

					<div class="subagent-profiles">
						<div class="text-xs text-gray-600 dark:text-gray-400">{$i18n.t('Profiles')}</div>
						<p class="mt-0.5 text-[0.6875rem] text-gray-400 dark:text-gray-600">
							{$i18n.t(
								'The main agent picks a profile when it delegates. A profile adds role instructions, and can run the sub-agent on another model.'
							)}
						</p>

						<div class="mt-2 flex flex-col gap-1">
							{#each builtinProfiles as profile (profile.name)}
								<div class="flex items-baseline gap-2 text-xs">
									<span class="font-mono text-gray-700 dark:text-gray-300 shrink-0"
										>{profile.name}</span
									>
									<span class="text-gray-400 dark:text-gray-600 min-w-0 truncate"
										>{profile.description}</span
									>
									<span
										class="ml-auto shrink-0 text-[0.625rem] uppercase tracking-wide text-gray-400 dark:text-gray-600"
										>{$i18n.t('Built-in')}</span
									>
								</div>
							{/each}
						</div>

						<div class="mt-2 flex flex-col gap-2">
							{#each profiles as profile, index}
								<div
									class="custom-profile flex flex-col gap-1.5 rounded-xl border border-gray-100 p-2.5 dark:border-white/[0.06]"
								>
									<div class="flex items-center gap-1.5">
										<input
											class="{inputClass} font-mono"
											placeholder={$i18n.t('Name, e.g. researcher')}
											aria-label={$i18n.t('Name')}
											bind:value={profile.name}
										/>
										<select
											class="{inputClass} max-w-[45%]"
											aria-label={$i18n.t('Model')}
											bind:value={profile.model}
										>
											<option value="">{$i18n.t('Same model as the main agent')}</option>
											{#each $models ?? [] as model (model.id)}
												<option value={model.id}>{model.name}</option>
											{/each}
										</select>
										<button
											type="button"
											class="shrink-0 rounded-lg px-2 py-1 text-xs text-gray-500 hover:bg-gray-50 hover:text-gray-800 dark:hover:bg-white/5 dark:hover:text-gray-200"
											on:click={() => removeProfile(index)}
										>
											{$i18n.t('Remove')}
										</button>
									</div>
									<input
										class={inputClass}
										placeholder={$i18n.t('When the main agent should use it')}
										aria-label={$i18n.t('Description')}
										bind:value={profile.description}
									/>
									<textarea
										rows="3"
										class="w-full resize-y rounded-lg border border-gray-100/50 bg-gray-50/40 px-2 py-1.5 text-xs text-gray-700 outline-hidden transition-colors focus:border-blue-400 dark:border-white/[0.04] dark:bg-white/[0.03] dark:text-gray-300 dark:focus:border-blue-500"
										placeholder={$i18n.t('Instructions for the sub-agent in this role')}
										aria-label={$i18n.t('Instructions')}
										bind:value={profile.prompt}
									></textarea>
								</div>
							{/each}
						</div>

						<button
							type="button"
							class="add-profile mt-2 rounded-lg px-2 py-1 text-xs text-gray-600 hover:bg-gray-50 hover:text-gray-900 dark:text-gray-400 dark:hover:bg-white/5 dark:hover:text-gray-100"
							on:click={addProfile}
						>
							+ {$i18n.t('Add profile')}
						</button>
					</div>

					{#if taskLimits.length > 0}
						<AdminSettingSection
							title={$i18n.t('settings.admin.subagents.tasks.title')}
							className="task-limits"
						>
							<p class="-mt-1 text-[0.6875rem] text-gray-400 dark:text-gray-600">
								{$i18n.t('settings.admin.subagents.tasks.description')}
							</p>
							{#each limitGroups as group}
								<div class="flex flex-col gap-1.5">
									<div
										class="pt-1 text-[0.625rem] font-medium uppercase tracking-wide text-gray-400 dark:text-gray-600"
									>
										{group.title}
									</div>
									{#each group.limits as limit (limit.name)}
										{@const view = limitViews[limit.name] ?? { label: limit.name, unit: '' }}
										{@const factor = limitFactor(limit.name)}
										<div class="flex items-center justify-between gap-3">
											<div class="min-w-0">
												<label
													class="text-xs text-gray-600 dark:text-gray-400"
													for="task-limit-{limit.name}"
												>
													{view.label}
												</label>
												{#if view.hint}
													<div class="text-[0.6875rem] text-gray-400 dark:text-gray-600">
														{view.hint}
													</div>
												{/if}
											</div>
											<div class="flex shrink-0 items-center gap-1.5">
												<input
													id="task-limit-{limit.name}"
													type="number"
													bind:value={taskLimitValues[limit.name]}
													min={limit.zero_allowed ? 0 : toDisplay(limit.name, limit.min)}
													max={toDisplay(limit.name, limit.max)}
													step={factor === 1 ? 1 : 'any'}
													placeholder={`${toDisplay(limit.name, limit.default)}`}
													title={$i18n.t('{{MIN}} to {{MAX}}, default {{DEFAULT}}', {
														MIN: toDisplay(limit.name, limit.min),
														MAX: toDisplay(limit.name, limit.max),
														DEFAULT: toDisplay(limit.name, limit.default)
													})}
													class="h-7 w-20 rounded-lg border border-gray-100/50 bg-gray-50/40 px-2 text-xs text-gray-700 outline-hidden transition-colors focus:border-blue-400 dark:border-white/[0.04] dark:bg-white/[0.03] dark:text-gray-300 dark:focus:border-blue-500"
												/>
												<span class="w-16 text-[0.6875rem] text-gray-400 dark:text-gray-600">
													{view.unit}
												</span>
											</div>
										</div>
									{/each}
								</div>
							{/each}
						</AdminSettingSection>
					{/if}
				{/if}
			</div>
		{/if}
	</div>

	{#if !loading}
		<div class="flex justify-end pt-6 text-sm font-normal">
			<button
				class="rounded-full bg-black px-3.5 py-1.5 text-sm font-normal text-white transition hover:bg-gray-900 disabled:opacity-50 dark:bg-white dark:text-black dark:hover:bg-gray-100"
				type="submit"
				disabled={saving}
			>
				{$i18n.t('Save')}
			</button>
		</div>
	{/if}
</form>
