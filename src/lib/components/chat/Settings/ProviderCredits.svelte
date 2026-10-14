<script lang="ts">
	// Mologate: credits (docs/limits-and-accounts.md, decision 7, phase 5). A member sees their
	// balance and what earned it; an admin edits the plan multipliers credits are granted by.
	import { onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import { user } from '$lib/stores';
	import { providerAccountRequest, type MyCredits } from '$lib/apis/provider-accounts';

	export let buttonClass = '';
	export let providers: Record<string, string> = {};
	export let planName: (plan: string) => string = (plan) => plan;

	let view: MyCredits | null = null;
	let editing = false;
	let table: Record<string, Record<string, number>> = {};
	let busy = false;

	const inputClass =
		'w-16 rounded-lg border border-gray-200 bg-transparent px-2 py-0.5 text-right dark:border-gray-700';
	const reasons: Record<string, string> = {
		grant: 'Earned',
		spend: 'Spent past your share',
		forfeit: 'Forfeited on disconnect',
		admin: 'Admin correction'
	};
	const amount = (value: number) =>
		Number.isInteger(value) ? String(value) : value.toFixed(2).replace(/\.?0+$/, '');

	const load = async () => {
		view = await providerAccountRequest<MyCredits>(localStorage.token, '/credits');
		table = JSON.parse(JSON.stringify(view.multipliers));
	};

	const save = async () => {
		busy = true;
		try {
			await providerAccountRequest(localStorage.token, '/credits/multipliers', 'PUT', {
				multipliers: table
			});
			editing = false;
			toast.success('Multipliers saved. They apply to accounts connected from now on.');
			await load();
		} catch (err) {
			toast.error(err instanceof Error ? err.message : 'The multipliers could not be saved.');
		} finally {
			busy = false;
		}
	};

	onMount(() => {
		load().catch(() => {
			// The section stays hidden; the rest of the page works.
		});
	});
</script>

{#if view}
	<section class="mt-4">
		<div class="mb-2 flex items-baseline justify-between gap-3">
			<h3 class="text-xs text-gray-600 dark:text-gray-400">Credits</h3>
			<span class="font-mono text-sm text-gray-900 dark:text-white">{amount(view.balance)}</span>
		</div>
		<p class="mb-2 text-[0.6875rem] text-gray-500">
			Adding an account to the pool earns 10 credits for each 1x of its plan, once per account; an
			API key earns {view.per_dollar} per dollar anyone spends through it. Past your share of the pool,
			credits keep big models available: one credit is 0.4% of the pool.
		</p>
		{#if view.grants.length || view.entries.length}
			<ul
				class="divide-y divide-gray-100 rounded-2xl border border-gray-100 text-xs dark:divide-gray-850 dark:border-gray-850"
			>
				{#each view.grants as grant}
					<li class="flex items-baseline justify-between gap-3 px-3 py-2">
						<span class="truncate">
							{providers[grant.provider] ?? grant.provider} · {planName(grant.plan)}
							{#if grant.forfeited_at}
								<span class="text-gray-500">· disconnected</span>
							{/if}
						</span>
						<span class="shrink-0 font-mono">+{amount(grant.amount)}</span>
					</li>
				{/each}
				{#each view.entries.filter((entry) => entry.reason !== 'grant').slice(0, 10) as entry}
					<li class="flex items-baseline justify-between gap-3 px-3 py-2 text-gray-500">
						<span class="truncate"
							>{reasons[entry.reason] ?? entry.reason} · {new Date(
								entry.ts * 1000
							).toLocaleDateString()}</span
						>
						<span class="shrink-0 font-mono">{entry.delta > 0 ? '+' : ''}{amount(entry.delta)}</span
						>
					</li>
				{/each}
			</ul>
		{/if}

		{#if $user?.role === 'admin'}
			<div class="mt-2 flex items-center justify-between gap-3">
				<span class="text-[0.6875rem] text-gray-500"
					>Plan multipliers (credits = 10 × multiplier)</span
				>
				<button type="button" class="{buttonClass} text-xs" on:click={() => (editing = !editing)}>
					{editing ? 'Cancel' : 'Edit'}
				</button>
			</div>
			{#if editing}
				<form
					class="mt-2 flex flex-col gap-2 rounded-2xl border border-gray-100 p-3 text-xs dark:border-gray-850"
					on:submit|preventDefault={save}
				>
					{#each Object.entries(table) as [provider, plans] (provider)}
						<div class="font-medium text-gray-900 dark:text-white">
							{providers[provider] ?? provider}
						</div>
						<div class="grid grid-cols-2 gap-x-4 gap-y-1">
							{#each Object.keys(plans) as plan (plan)}
								<label class="flex items-center justify-between gap-2">
									<span>{planName(plan)}</span>
									<input
										class={inputClass}
										type="number"
										min="0"
										max="1000"
										step="0.5"
										bind:value={table[provider][plan]}
									/>
								</label>
							{/each}
						</div>
					{/each}
					<div class="flex justify-end">
						<button type="submit" class={buttonClass} disabled={busy}>Save</button>
					</div>
				</form>
			{/if}
		{/if}
	</section>
{/if}
