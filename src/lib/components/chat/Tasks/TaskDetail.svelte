<script lang="ts">
	import { getContext, onDestroy } from 'svelte';
	import { get, type Readable } from 'svelte/store';

	import {
		getChatTask,
		type Task,
		type TaskCheck,
		type TaskDetail,
		type TaskRun
	} from '$lib/apis/chatTasks';
	import { formatMessageTimestamp } from '$lib/utils';

	import Markdown from '../Messages/Markdown.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import CheckCircle from '$lib/components/icons/CheckCircle.svelte';
	import ChevronDown from '$lib/components/icons/ChevronDown.svelte';
	import ChevronLeft from '$lib/components/icons/ChevronLeft.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';
	import TaskIcon from './TaskIcon.svelte';
	import TaskStatus from './TaskStatus.svelte';
	import { formatSeconds, timeText, typeLabel } from './format';
	import { chatTasks, isActiveTask, now, type ChatTasksState } from './tasksStore';

	const i18n =
		getContext<Readable<{ t: (key: string, options?: Record<string, unknown>) => string }>>('i18n');

	export let chatId: string;
	// The task as the list has it; the detail adds its output, prompt and history.
	export let task: Task;
	export let history: any = null;
	export let showMessage: Function | null = null;
	export let stopping = false;
	export let onStop: () => void = () => {};
	export let onBack: () => void = () => {};

	// A running task's output is polled, since it changes between the backend's nudges.
	const POLL_INTERVAL = 2000;

	let detail: TaskDetail | null = null;
	let error = '';
	let promptOpen = false;
	let pollTimer: ReturnType<typeof setTimeout> | null = null;
	let lastRequest = 0;
	let destroyed = false;
	let loadedId = '';
	let lastStatus = '';
	let lastEventAt = get(chatTasks).event?.at ?? 0;

	const load = async () => {
		const id = loadedId;
		const request = ++lastRequest;
		if (pollTimer) clearTimeout(pollTimer);
		pollTimer = null;
		try {
			const res = await getChatTask(localStorage.token, chatId, id);
			if (destroyed || request !== lastRequest) return;
			detail = res;
			error = '';
		} catch (err) {
			if (destroyed || request !== lastRequest) return;
			if (!detail) error = (err as Error)?.message ?? `${err}`;
		}
		if ((detail?.status ?? task.status) === 'running') {
			pollTimer = setTimeout(load, POLL_INTERVAL);
		}
	};

	// Another task opened, or this one's status moved in the list: fetch it again.
	const onTask = (value: Task) => {
		if (value.id !== loadedId) {
			loadedId = value.id;
			lastStatus = value.status;
			detail = null;
			error = '';
			promptOpen = false;
			void load();
		} else if (value.status !== lastStatus) {
			lastStatus = value.status;
			void load();
		}
	};
	$: onTask(task);

	const onEvent = (event: ChatTasksState['event']) => {
		if (!event || event.at === lastEventAt) return;
		lastEventAt = event.at;
		if (event.task_id === loadedId) void load();
	};
	$: onEvent($chatTasks.event);

	onDestroy(() => {
		destroyed = true;
		if (pollTimer) clearTimeout(pollTimer);
	});

	$: t = (key: string, options?: Record<string, unknown>) => $i18n.t(key, options);
	$: shown = detail?.id === task.id ? detail : null;
	$: current = (shown ?? task) as Task;
	$: title = current.title || typeLabel(current.type, t);
	$: time = timeText(current, $now, t);
	$: canStop = current.can_stop && isActiveTask(current);
	$: messages = history?.messages ?? {};
	$: startMessage = current.message_id ? messages[current.message_id] : null;
	$: runs = [...(shown?.runs ?? [])].sort((a, b) => b.started_at - a.started_at);
	$: checks = [...(shown?.checks ?? [])].sort((a, b) => b.at - a.at);
	$: activity = shown?.activity ?? [];
	$: prompt = shown?.prompt?.trim() ?? '';
	$: promptPreview = prompt.split('\n').find((line) => line.trim()) ?? '';
	$: meta = buildMeta(current, t);

	type MetaItem = { label: string; value: string; mono?: boolean };

	const buildMeta = (
		value: Task,
		t: (key: string, options?: Record<string, unknown>) => string
	) => {
		const items: (MetaItem | null)[] = [
			{
				label: t('Type'),
				value: value.profile
					? `${typeLabel(value.type, t)} · ${value.profile}`
					: typeLabel(value.type, t)
			},
			value.mode === 'background'
				? { label: t('Mode'), value: t('In the background') }
				: value.mode === 'foreground'
					? { label: t('Mode'), value: t('The agent waits for it') }
					: null,
			{
				label: value.started_at ? t('Started') : t('Created'),
				value: formatMessageTimestamp(value.started_at ?? value.created_at)
			},
			value.finished_at
				? { label: t('Finished'), value: formatMessageTimestamp(value.finished_at) }
				: null
		];
		const schedule = value.schedule;
		if (schedule) {
			items.push(
				{
					label: t('Repeats'),
					value: schedule.every
						? t('Every {{TIME}}', { TIME: formatSeconds(schedule.every) })
						: t('Once')
				},
				value.status === 'scheduled' && schedule.next_run_at
					? { label: t('Next run'), value: formatMessageTimestamp(schedule.next_run_at) }
					: null,
				{
					label: t('Runs'),
					value: schedule.max_runs
						? t('{{COUNT}} of {{MAX}}', { COUNT: schedule.runs, MAX: schedule.max_runs })
						: `${schedule.runs}`
				},
				schedule.ends_at
					? { label: t('Ends'), value: formatMessageTimestamp(schedule.ends_at) }
					: null,
				schedule.failures ? { label: t('Failed in a row'), value: `${schedule.failures}` } : null
			);
		}
		const monitor = value.monitor;
		if (monitor) {
			items.push(
				{
					label: monitor.kind === 'http' ? t('URL') : t('Command'),
					value: monitor.target,
					mono: true
				},
				{ label: t('Checks every'), value: formatSeconds(monitor.interval) },
				{
					label: t('Checks'),
					value: t('{{COUNT}} of {{MAX}}', { COUNT: monitor.checks, MAX: monitor.max_checks })
				},
				monitor.ends_at
					? { label: t('Gives up'), value: formatMessageTimestamp(monitor.ends_at) }
					: null
			);
		}
		items.push({ label: t('ID'), value: value.handle, mono: true });
		return items.filter((item): item is MetaItem => !!item);
	};

	const RUN_DOTS: Record<string, string> = {
		completed: 'bg-emerald-500',
		failed: 'bg-red-400',
		running: 'bg-lime-500 dark:bg-accent animate-pulse'
	};

	const runLabel = (run: TaskRun) =>
		({
			completed: t('Completed'),
			failed: t('Failed'),
			running: t('Running'),
			stopped: t('Stopped'),
			skipped: t('Skipped')
		})[run.status] ?? run.status;

	const runTime = (run: TaskRun, nowMs: number) =>
		formatSeconds((run.finished_at ?? nowMs / 1000) - run.started_at);

	const checkTime = (check: TaskCheck) =>
		new Date(check.at * 1000).toLocaleTimeString(undefined, {
			hour: 'numeric',
			minute: '2-digit',
			second: '2-digit'
		});

	const activityState = (status: string) =>
		['failed', 'error'].includes(status)
			? 'failed'
			: ['in_progress', 'running', 'pending', 'executing'].includes(status) &&
				  current.status === 'running'
				? 'running'
				: 'done';

	const show = (messageId: string | null) => {
		const message = messageId ? messages[messageId] : null;
		if (message && showMessage) showMessage(message, true);
	};
</script>

<div class="flex h-full min-h-0 flex-col">
	<div
		class="flex h-8 shrink-0 items-center gap-1.5 border-b border-gray-50 pl-1.5 pr-2 dark:border-gray-850/30"
	>
		<Tooltip content={$i18n.t('All tasks')}>
			<button
				type="button"
				class="flex size-6 shrink-0 items-center justify-center rounded-md text-gray-400 transition hover:bg-gray-50 hover:text-gray-700 outline-hidden focus-visible:ring-2 focus-visible:ring-gray-900 dark:text-gray-500 dark:hover:bg-white/5 dark:hover:text-gray-200 dark:focus-visible:ring-accent"
				on:click={onBack}
				aria-label={$i18n.t('All tasks')}
			>
				<ChevronLeft className="size-3.5" strokeWidth="2" />
			</button>
		</Tooltip>
		<span class="shrink-0 text-gray-400 dark:text-gray-500">
			<TaskIcon type={current.type} className="size-3.5" />
		</span>
		<h3
			class="min-w-0 flex-1 truncate text-xs font-normal text-gray-700 dark:text-gray-300"
			{title}
		>
			{title}
		</h3>
		{#if canStop}
			<button
				type="button"
				class="flex h-6 shrink-0 items-center gap-1 rounded-md border border-gray-100 px-2 text-[0.6875rem] text-gray-600 transition hover:bg-gray-50 hover:text-gray-900 outline-hidden focus-visible:ring-2 focus-visible:ring-gray-900 disabled:opacity-50 dark:border-white/[0.06] dark:text-gray-300 dark:hover:bg-gray-850 dark:hover:text-white dark:focus-visible:ring-accent"
				disabled={stopping}
				on:click={onStop}
				aria-label={$i18n.t('Stop {{NAME}}', { NAME: title })}
			>
				{#if stopping}
					<Spinner className="size-3" />
				{/if}
				{$i18n.t('Stop')}
			</button>
		{/if}
	</div>

	<div class="task-detail min-h-0 flex-1 overflow-y-auto scrollbar-hidden pb-4">
		<div class="px-3 pt-3 pb-2">
			<div
				class="flex min-w-0 items-center gap-1.5 text-[0.6875rem] text-gray-400 dark:text-gray-500"
			>
				<TaskStatus status={current.status} />
				{#if time}
					<span class="min-w-0 truncate tabular-nums">{time}</span>
				{/if}
				{#if current.type === 'subagent' && current.conversation_id}
					<a
						href={`/c/${current.conversation_id}`}
						class="ml-auto shrink-0 text-[0.6875rem] text-gray-400 transition-colors hover:text-gray-700 hover:underline dark:text-gray-500 dark:hover:text-gray-300"
					>
						{$i18n.t('Open conversation')}
					</a>
				{/if}
			</div>

			<dl
				class="mt-2.5 grid grid-cols-[max-content_minmax(0,1fr)] gap-x-3 gap-y-1 text-[0.6875rem]"
			>
				{#each meta as item}
					<dt class="text-gray-400 dark:text-gray-500">{item.label}</dt>
					<dd
						class="min-w-0 break-words text-gray-700 dark:text-gray-300 {item.mono
							? 'font-mono'
							: ''}"
					>
						{item.value}
					</dd>
				{/each}
			</dl>

			{#if startMessage && showMessage}
				<button
					type="button"
					class="mt-2 text-[0.6875rem] text-gray-400 transition-colors hover:text-gray-700 hover:underline dark:text-gray-500 dark:hover:text-gray-300"
					on:click={() => show(current.message_id)}
				>
					{$i18n.t('Show in chat')}
				</button>
			{/if}
		</div>

		{#if current.error}
			<div
				class="mx-3 mb-2 whitespace-pre-wrap break-words rounded-lg bg-red-50 px-2.5 py-1.5 text-[0.6875rem] text-red-600 dark:bg-red-500/10 dark:text-red-400"
			>
				{current.error}
			</div>
		{/if}

		{#if prompt}
			<div class="px-3 pb-2">
				<button
					type="button"
					class="flex w-full min-w-0 items-center gap-1.5 text-left text-[0.6875rem] text-gray-400 transition-colors hover:text-gray-600 dark:text-gray-500 dark:hover:text-gray-300"
					aria-expanded={promptOpen}
					on:click={() => (promptOpen = !promptOpen)}
				>
					<span class="shrink-0">{$i18n.t('Prompt')}</span>
					{#if !promptOpen}
						<span class="min-w-0 truncate text-gray-500 dark:text-gray-400">{promptPreview}</span>
					{/if}
					<ChevronDown
						className="size-3 shrink-0 transition-transform duration-150 {promptOpen
							? 'rotate-180'
							: ''}"
					/>
				</button>
				{#if promptOpen}
					<div
						class="mt-1.5 whitespace-pre-wrap break-words border-l border-gray-100 pl-3 text-xs leading-relaxed text-gray-600 dark:border-white/8 dark:text-gray-400"
					>
						{prompt}
					</div>
				{/if}
			</div>
		{/if}

		<hr class="my-1.5 border-gray-50/60 dark:border-gray-850/25" />

		<section class="px-3 py-2">
			<div
				class="mb-1.5 flex items-center gap-1.5 text-[0.6875rem] text-gray-400 dark:text-gray-500"
			>
				{$i18n.t('Output')}
				{#if shown?.live}
					<span class="inline-flex items-center gap-1 text-lime-700 dark:text-accent">
						<span class="size-1.5 animate-pulse rounded-full bg-current" aria-hidden="true"></span>
						{$i18n.t('Live')}
					</span>
				{/if}
			</div>
			{#if !shown && !error}
				<div class="flex justify-center py-4"><Spinner className="size-4" /></div>
			{:else if !shown}
				<div class="text-[0.6875rem] text-gray-400 dark:text-gray-600">{error}</div>
			{:else if shown.output}
				{#if current.type === 'monitor'}
					<pre
						class="max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-gray-50 p-2 font-mono text-[0.6875rem] leading-relaxed text-gray-700 dark:bg-gray-850 dark:text-gray-300">{shown.output}</pre>
				{:else}
					<div
						class="task-output prose prose-sm dark:prose-invert max-w-none break-words text-[0.8125rem] leading-relaxed prose-p:my-1.5 prose-headings:mt-3 prose-headings:mb-1 prose-headings:font-medium prose-h1:text-base prose-h2:text-sm prose-h3:text-sm prose-strong:font-medium prose-ul:my-1.5 prose-ol:my-1.5 prose-li:my-0.5 prose-pre:my-2 prose-code:before:content-none prose-code:after:content-none [&>:first-child]:mt-0 [&>:last-child]:mb-0"
					>
						<Markdown
							id={`task-${current.id}-output`}
							content={shown.output}
							done={!shown.live}
							editCodeBlock={false}
						/>
					</div>
				{/if}
			{:else}
				<div class="text-[0.6875rem] text-gray-400 dark:text-gray-600">
					{current.status === 'running'
						? $i18n.t('No output yet')
						: current.status === 'scheduled'
							? $i18n.t('Not run yet')
							: $i18n.t('No output')}
				</div>
			{/if}
		</section>

		{#if activity.length > 0}
			<hr class="my-1.5 border-gray-50/60 dark:border-gray-850/25" />
			<section class="px-3 py-2">
				<div class="mb-1 text-[0.6875rem] text-gray-400 dark:text-gray-500">
					{$i18n.t('Activity')}
				</div>
				<ul>
					{#each activity as item}
						{@const itemState = activityState(item.status)}
						<li class="flex h-6 min-w-0 items-center gap-2 text-[0.6875rem]">
							{#if itemState === 'running'}
								<Spinner className="size-3 text-gray-400" />
							{:else if itemState === 'failed'}
								<span class="text-red-500 dark:text-red-400"><XMark className="size-3" /></span>
							{:else}
								<span class="text-emerald-500 dark:text-emerald-400">
									<CheckCircle className="size-3" strokeWidth="2" />
								</span>
							{/if}
							<span class="min-w-0 truncate font-mono text-gray-600 dark:text-gray-300"
								>{item.name}</span
							>
						</li>
					{/each}
				</ul>
			</section>
		{/if}

		{#if shown && current.type === 'scheduled'}
			<hr class="my-1.5 border-gray-50/60 dark:border-gray-850/25" />
			<section class="px-3 py-2">
				<div class="mb-1 text-[0.6875rem] text-gray-400 dark:text-gray-500">
					{$i18n.t('Runs')}
				</div>
				{#if runs.length === 0}
					<div class="py-1 text-[0.6875rem] text-gray-400 dark:text-gray-600">
						{$i18n.t('No runs yet')}
					</div>
				{:else}
					<ul>
						{#each runs as run}
							<li class="flex h-7 min-w-0 items-center gap-2 text-xs">
								<span
									class="size-1.5 shrink-0 rounded-full {RUN_DOTS[run.status] ??
										'bg-gray-300 dark:bg-gray-600'}"
									aria-hidden="true"
								></span>
								<span class="shrink-0 tabular-nums text-gray-500 dark:text-gray-400">
									{formatMessageTimestamp(run.started_at)}
								</span>
								<span class="min-w-0 truncate text-[0.6875rem] text-gray-400 dark:text-gray-500">
									{runLabel(run)} · {runTime(run, $now)}
								</span>
								{#if run.message_id && messages[run.message_id] && showMessage}
									<button
										type="button"
										class="ml-auto shrink-0 text-[0.6875rem] text-gray-400 transition-colors hover:text-gray-700 hover:underline dark:text-gray-500 dark:hover:text-gray-300"
										on:click={() => show(run.message_id)}
									>
										{$i18n.t('Show in chat')}
									</button>
								{/if}
							</li>
							{#if run.error}
								<li
									class="-mt-1 truncate pb-1 pl-3.5 text-[0.6875rem] text-red-400"
									title={run.error}
								>
									{run.error}
								</li>
							{/if}
						{/each}
					</ul>
				{/if}
			</section>
		{/if}

		{#if shown && current.type === 'monitor'}
			<hr class="my-1.5 border-gray-50/60 dark:border-gray-850/25" />
			<section class="px-3 py-2">
				<div class="mb-1 text-[0.6875rem] text-gray-400 dark:text-gray-500">
					{(current.monitor?.checks ?? 0) > checks.length
						? $i18n.t('Latest checks')
						: $i18n.t('Checks')}
				</div>
				{#if checks.length === 0}
					<div class="py-1 text-[0.6875rem] text-gray-400 dark:text-gray-600">
						{$i18n.t('No checks yet')}
					</div>
				{:else}
					<ul>
						{#each checks as check}
							<li class="flex h-6 min-w-0 items-center gap-2 text-[0.6875rem]">
								<span
									class="size-1.5 shrink-0 rounded-full {check.met
										? 'bg-emerald-500'
										: 'bg-gray-300 dark:bg-gray-600'}"
									aria-hidden="true"
								></span>
								<span class="shrink-0 tabular-nums text-gray-500 dark:text-gray-400">
									{checkTime(check)}
								</span>
								<span class="min-w-0 truncate text-gray-400 dark:text-gray-500">
									{check.met ? $i18n.t('Condition met') : $i18n.t('Not yet')}
								</span>
								{#if check.exit_code !== null || check.http_status !== null}
									<span class="ml-auto shrink-0 font-mono text-gray-400 dark:text-gray-500">
										{check.http_status !== null
											? `HTTP ${check.http_status}`
											: `exit ${check.exit_code}`}
									</span>
								{/if}
							</li>
						{/each}
					</ul>
				{/if}
			</section>
		{/if}
	</div>
</div>
