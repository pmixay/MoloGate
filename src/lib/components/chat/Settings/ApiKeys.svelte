<script lang="ts">
	// Mologate: "My keys" (docs/limits-and-accounts.md §11, phase 6). A member's personal API keys
	// for outside OpenAI-compatible clients: several named keys, each optionally held to fewer
	// requests, models, efforts or pool points than its owner. Every request a key makes is the
	// owner's, counted in their share of the pool. A key is shown once, when it is made.
	import { onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import { models } from '$lib/stores';
	import {
		apiKeysRequest,
		type KeyBounds,
		type KeyLimits,
		type PersonalKey
	} from '$lib/apis/api-keys';
	import { copyToClipboard } from '$lib/utils';
	import { formatCount, formatDollars } from '$lib/utils/providerLimits';

	const buttonClass =
		'rounded-full border border-gray-200 px-3 py-1 font-medium text-gray-700 transition hover:bg-gray-50 disabled:opacity-50 dark:border-gray-700 dark:text-gray-200 dark:hover:bg-gray-800';
	const inputClass =
		'w-full rounded-lg border border-gray-200 bg-transparent px-2 py-1 dark:border-gray-700';
	const providers: Record<string, string> = {
		codex: 'ChatGPT',
		anthropic_api: 'Anthropic',
		openai_api: 'OpenAI',
		openrouter: 'OpenRouter',
		zai: 'Z.ai'
	};

	let keys: PersonalKey[] = [];
	let bounds: KeyBounds | null = null;
	let loading = true;
	let busy = false;
	let formOpen = false;
	let editingId = '';
	let revokingId = '';
	let created: { name: string; secret: string } | null = null;

	// The form, for a new key or to change one.
	let name = '';
	let days: number | null = null;
	let rpm: number | null = null;
	let concurrency: number | null = null;
	let daily: number | null = null;
	let chosenModels: string[] = [];
	let chosenEfforts: string[] = [];

	$: modelOptions = ($models ?? [])
		.filter((model: any) => !(model?.info?.meta?.hidden ?? false))
		.map((model: any) => ({ id: model.id, name: model.name ?? model.id }));
	$: baseUrl = typeof window === 'undefined' ? '/api' : `${window.location.origin}/api`;

	const load = async () => {
		const result = await apiKeysRequest<{ data: PersonalKey[]; bounds: KeyBounds }>(
			localStorage.token
		);
		keys = result.data;
		bounds = result.bounds;
	};

	const resetForm = (key: PersonalKey | null = null) => {
		editingId = key?.id ?? '';
		name = key?.name ?? '';
		days = null;
		rpm = key?.limits.rpm ?? null;
		concurrency = key?.limits.concurrency ?? null;
		daily = key?.limits.daily_points ?? null;
		chosenModels = [...(key?.limits.models ?? [])];
		chosenEfforts = [...(key?.limits.efforts ?? [])];
	};

	const limits = (): KeyLimits => ({
		...(rpm ? { rpm: Number(rpm) } : {}),
		...(concurrency ? { concurrency: Number(concurrency) } : {}),
		...(daily ? { daily_points: Number(daily) } : {}),
		...(chosenModels.length ? { models: chosenModels } : {}),
		...(chosenEfforts.length ? { efforts: chosenEfforts } : {})
	});

	const save = async () => {
		busy = true;
		try {
			if (editingId) {
				await apiKeysRequest(localStorage.token, `/${editingId}`, 'PATCH', {
					name,
					limits: limits()
				});
				toast.success('Key updated.');
			} else {
				const result = await apiKeysRequest<{ key: PersonalKey; secret: string }>(
					localStorage.token,
					'',
					'POST',
					{ name, limits: limits(), ...(days ? { expires_in_days: Number(days) } : {}) }
				);
				created = { name: result.key.name, secret: result.secret };
			}
			formOpen = false;
			resetForm();
			await load();
		} catch (err) {
			toast.error(err instanceof Error ? err.message : 'The key could not be saved.');
		} finally {
			busy = false;
		}
	};

	const revoke = async (key: PersonalKey) => {
		busy = true;
		try {
			await apiKeysRequest(localStorage.token, `/${key.id}`, 'DELETE');
			revokingId = '';
			await load();
		} catch (err) {
			toast.error(err instanceof Error ? err.message : 'The key could not be revoked.');
		} finally {
			busy = false;
		}
	};

	const when = (ts: number | null) => (ts ? new Date(ts * 1000).toLocaleDateString() : '');
	const status = (key: PersonalKey) =>
		key.revoked_at
			? `Revoked ${when(key.revoked_at)}`
			: key.expires_at && key.expires_at * 1000 < Date.now()
				? `Expired ${when(key.expires_at)}`
				: key.expires_at
					? `Until ${when(key.expires_at)}`
					: '';
	const limitText = (value: KeyLimits) =>
		[
			value.rpm ? `${value.rpm}/min` : '',
			value.concurrency ? `${value.concurrency} at once` : '',
			value.daily_points ? `${value.daily_points} pool points a day` : '',
			value.models?.length
				? `${value.models.length} ${value.models.length === 1 ? 'model' : 'models'}`
				: '',
			value.efforts?.length ? `effort ${value.efforts.join(', ')}` : ''
		]
			.filter(Boolean)
			.join(' · ') || 'Your own limits';
	const useText = (key: PersonalKey) => {
		const today = key.usage?.today;
		const week = key.usage?.week;
		if (!week) return 'Not used this week';
		const parts = [
			`${week.requests} requests this week`,
			`${formatCount(week.input_tokens)} in · ${formatCount(week.output_tokens)} out`
		];
		if (week.cost != null) parts.push(formatDollars(week.cost));
		if (today) parts.push(`${today.requests} today`);
		const points = Object.entries(key.usage?.points_today ?? {})
			.map(([provider, value]) => `${value} points of ${providers[provider] ?? provider} today`)
			.join(', ');
		if (points) parts.push(points);
		return parts.join(' · ');
	};

	onMount(() => {
		load()
			.catch((err) => toast.error(err instanceof Error ? err.message : 'Keys could not load.'))
			.finally(() => (loading = false));
	});
</script>

<div class="flex h-full flex-col text-sm">
	<div class="mb-1 flex items-center justify-between gap-3">
		<h2 class="text-base font-medium text-gray-900 dark:text-white">API keys</h2>
		<button
			type="button"
			class="rounded-full bg-black px-3 py-1 text-xs font-medium text-white transition hover:bg-gray-900 dark:bg-white dark:text-black dark:hover:bg-gray-100"
			on:click={() => {
				resetForm();
				formOpen = !formOpen || !!editingId;
			}}>{formOpen && !editingId ? 'Cancel' : 'New key'}</button
		>
	</div>
	<p class="mb-3 text-xs text-gray-500">
		Use Mologate from any OpenAI-compatible client: base URL <code class="font-mono">{baseUrl}</code
		>, your key, and a model id from the list. Everything a key does counts in your share of the
		team pool. A key reaches only chat completions, the model list and your limits.
	</p>

	{#if created}
		<div
			class="mb-3 rounded-2xl border border-emerald-200 bg-emerald-50 p-3 text-xs text-emerald-900 dark:border-emerald-500/30 dark:bg-emerald-400/10 dark:text-emerald-200"
			role="status"
		>
			<div class="mb-1 font-medium">
				{created.name} is ready. Copy it now: it is shown only once.
			</div>
			<div class="flex items-center gap-2">
				<code class="min-w-0 flex-1 truncate font-mono">{created.secret}</code>
				<button
					type="button"
					class={buttonClass}
					on:click={async () => {
						if (created && (await copyToClipboard(created.secret))) toast.success('Copied.');
					}}>Copy</button
				>
				<button type="button" class={buttonClass} on:click={() => (created = null)}>Done</button>
			</div>
		</div>
	{/if}

	{#if formOpen}
		<form
			class="mb-3 grid grid-cols-2 gap-2 rounded-2xl border border-gray-100 p-3 text-xs dark:border-gray-850"
			on:submit|preventDefault={save}
		>
			<label class="col-span-2 flex flex-col gap-1">
				<span class="text-gray-500">Name</span>
				<input class={inputClass} maxlength="80" required bind:value={name} placeholder="Laptop" />
			</label>
			{#if !editingId}
				<label class="flex flex-col gap-1">
					<span class="text-gray-500">Expires after days (empty: never)</span>
					<input class={inputClass} type="number" min="1" max="3650" bind:value={days} />
				</label>
			{/if}
			<label class="flex flex-col gap-1">
				<span class="text-gray-500">Requests a minute</span>
				<input class={inputClass} type="number" min="1" max={bounds?.rpm} bind:value={rpm} />
			</label>
			<label class="flex flex-col gap-1">
				<span class="text-gray-500">At once (up to {bounds?.concurrency ?? 3}, as you)</span>
				<input
					class={inputClass}
					type="number"
					min="1"
					max={bounds?.concurrency}
					bind:value={concurrency}
				/>
			</label>
			<label class="flex flex-col gap-1">
				<span class="text-gray-500">Pool points a day</span>
				<input class={inputClass} type="number" min="0.1" max="100" step="0.1" bind:value={daily} />
			</label>
			<fieldset class="col-span-2 flex flex-col gap-1">
				<legend class="mb-1 text-gray-500">Efforts (none ticked: any)</legend>
				<div class="flex flex-wrap gap-3">
					{#each bounds?.efforts ?? [] as effort}
						<label class="flex items-center gap-1">
							<input type="checkbox" value={effort} bind:group={chosenEfforts} />
							{effort}
						</label>
					{/each}
				</div>
			</fieldset>
			<label class="col-span-2 flex flex-col gap-1">
				<span class="text-gray-500">Models (none chosen: any you can use)</span>
				<select class="{inputClass} h-24" multiple bind:value={chosenModels}>
					{#each modelOptions as model (model.id)}
						<option value={model.id}>{model.name}</option>
					{/each}
				</select>
			</label>
			<div class="col-span-2 flex justify-end gap-2">
				{#if editingId}
					<button
						type="button"
						class={buttonClass}
						on:click={() => {
							formOpen = false;
							resetForm();
						}}>Cancel</button
					>
				{/if}
				<button type="submit" class={buttonClass} disabled={busy || !name.trim()}
					>{editingId ? 'Save' : 'Make key'}</button
				>
			</div>
		</form>
	{/if}

	{#if loading}
		<p class="text-xs text-gray-500">Loading…</p>
	{:else if keys.length}
		<ul
			class="divide-y divide-gray-100 rounded-2xl border border-gray-100 text-xs dark:divide-gray-850 dark:border-gray-850"
		>
			{#each keys as key (key.id)}
				<li class="flex flex-col gap-1 px-3 py-2 {key.revoked_at ? 'opacity-60' : ''}">
					<div class="flex items-baseline justify-between gap-3">
						<span class="truncate font-medium text-gray-900 dark:text-white">
							{key.name}
							{#if key.hint}<span class="ml-1 font-mono font-normal text-gray-500">{key.hint}</span
								>{/if}
						</span>
						<span class="shrink-0 text-[0.6875rem] text-gray-500">
							Made {when(key.created_at)}{key.last_used_at
								? ` · used ${when(key.last_used_at)}`
								: ''}{status(key) ? ` · ${status(key)}` : ''}
						</span>
					</div>
					<div class="text-[0.6875rem] text-gray-500">{limitText(key.limits)}</div>
					<div class="text-[0.6875rem] text-gray-500">{useText(key)}</div>
					{#if !key.revoked_at}
						<div class="mt-1 flex justify-end gap-2">
							{#if revokingId === key.id}
								<span class="self-center text-[0.6875rem] text-gray-500"
									>Clients using it stop working.</span
								>
								<button
									type="button"
									class="rounded-full bg-red-600 px-3 py-1 font-medium text-white transition hover:bg-red-700 disabled:opacity-50"
									disabled={busy}
									on:click={() => revoke(key)}>Revoke</button
								>
								<button type="button" class={buttonClass} on:click={() => (revokingId = '')}
									>Keep</button
								>
							{:else}
								<button
									type="button"
									class={buttonClass}
									on:click={() => {
										resetForm(key);
										formOpen = true;
									}}>Edit</button
								>
								<button type="button" class={buttonClass} on:click={() => (revokingId = key.id)}
									>Revoke</button
								>
							{/if}
						</div>
					{/if}
				</li>
			{/each}
		</ul>
	{:else}
		<p class="text-xs text-gray-500">No keys yet.</p>
	{/if}
</div>
