<script lang="ts">
	// Mologate: admin and team overrides of the fair-use share (docs/limits-and-accounts.md,
	// phase 4). An override adds pool points to a member's share, or to every member's, until it
	// ends or an admin revokes it. Revoked and ended ones stay listed as the audit log.
	import { onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import { getAllUsers } from '$lib/apis/users';
	import { providerAccountRequest, type LimitOverride } from '$lib/apis/provider-accounts';

	export let buttonClass = '';

	let overrides: LimitOverride[] = [];
	let members: { id: string; name: string }[] = [];
	let memberId = '';
	let points = 5;
	let days: number | null = 7;
	let reason = '';
	let busy = false;
	let open = false;

	const inputClass =
		'w-full rounded-lg border border-gray-200 bg-transparent px-2 py-1 dark:border-gray-700';

	const request = <T,>(path = '', method = 'GET', body?: object) =>
		providerAccountRequest<T>(localStorage.token, `/overrides${path}`, method, body);

	const load = async () => {
		overrides = (await request<{ data: LimitOverride[] }>()).data;
	};

	const state = (item: LimitOverride, now = Date.now() / 1000) =>
		item.revoked_at
			? `Revoked by ${item.revoked_by_name ?? 'an admin'}`
			: item.until && item.until <= now
				? 'Ended'
				: item.until
					? `Until ${new Date(item.until * 1000).toLocaleDateString()}`
					: 'Until revoked';
	const inForce = (item: LimitOverride) =>
		!item.revoked_at && (!item.until || item.until > Date.now() / 1000);

	const grant = async () => {
		busy = true;
		try {
			await request('', 'POST', {
				user_id: memberId || null,
				extra_points: Number(points),
				until: days ? Math.floor(Date.now() / 1000) + Number(days) * 86400 : null,
				reason
			});
			reason = '';
			open = false;
			toast.success('Override granted.');
			await load();
		} catch (err) {
			toast.error(err instanceof Error ? err.message : 'The override could not be granted.');
		} finally {
			busy = false;
		}
	};

	const revoke = async (item: LimitOverride) => {
		busy = true;
		try {
			await request(`/${item.id}`, 'DELETE');
			await load();
		} catch (err) {
			toast.error(err instanceof Error ? err.message : 'The override could not be revoked.');
		} finally {
			busy = false;
		}
	};

	onMount(async () => {
		try {
			await load();
			const result = await getAllUsers(localStorage.token);
			members = (result?.users ?? [])
				.filter((member: any) => member.role !== 'pending')
				.map((member: any) => ({ id: member.id, name: member.name }));
		} catch {
			// The section stays empty; the rest of the page works.
		}
	});
</script>

<section class="mt-4">
	<div class="mb-2 flex items-center justify-between gap-3">
		<h3 class="text-xs text-gray-600 dark:text-gray-400">Overrides</h3>
		<button type="button" class="{buttonClass} text-xs" on:click={() => (open = !open)}>
			{open ? 'Cancel' : 'Grant'}
		</button>
	</div>
	{#if open}
		<form
			class="mb-2 grid grid-cols-2 gap-2 rounded-2xl border border-gray-100 p-3 text-xs dark:border-gray-850"
			on:submit|preventDefault={grant}
		>
			<label class="col-span-2 flex flex-col gap-1">
				<span class="text-gray-500">For</span>
				<select class={inputClass} bind:value={memberId}>
					<option value="">The whole team</option>
					{#each members as member (member.id)}
						<option value={member.id}>{member.name}</option>
					{/each}
				</select>
			</label>
			<label class="flex flex-col gap-1">
				<span class="text-gray-500">Extra pool points</span>
				<input
					class={inputClass}
					type="number"
					min="0.1"
					max="100"
					step="0.1"
					bind:value={points}
				/>
			</label>
			<label class="flex flex-col gap-1">
				<span class="text-gray-500">For how many days (empty: until revoked)</span>
				<input class={inputClass} type="number" min="1" step="1" bind:value={days} />
			</label>
			<label class="col-span-2 flex flex-col gap-1">
				<span class="text-gray-500">Reason</span>
				<input class={inputClass} maxlength="500" required bind:value={reason} />
			</label>
			<div class="col-span-2 flex justify-end">
				<button type="submit" class={buttonClass} disabled={busy || !reason.trim()}>Grant</button>
			</div>
		</form>
	{/if}
	{#if overrides.length}
		<ul
			class="divide-y divide-gray-100 rounded-2xl border border-gray-100 text-xs dark:divide-gray-850 dark:border-gray-850"
		>
			{#each overrides as item (item.id)}
				<li class="flex items-start justify-between gap-3 px-3 py-2">
					<div class="min-w-0">
						<div class="font-medium text-gray-900 dark:text-white">
							{item.user_name} · +{item.extra_points} points
						</div>
						<div class="text-[0.6875rem] text-gray-500">
							{item.reason} · granted by {item.granted_by_name}
							{new Date(item.created_at * 1000).toLocaleDateString()} · {state(item)}
						</div>
					</div>
					{#if inForce(item)}
						<button
							type="button"
							class="{buttonClass} shrink-0"
							disabled={busy}
							on:click={() => revoke(item)}>Revoke</button
						>
					{/if}
				</li>
			{/each}
		</ul>
	{:else}
		<p class="text-xs text-gray-500">
			No overrides. An override adds points of the team pool to a member's share, or to everyone's.
		</p>
	{/if}
</section>
